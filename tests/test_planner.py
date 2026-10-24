"""Scheduling-engine seam: contract, request building, every adapter, fallback."""

from __future__ import annotations

import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from transit_mcp.planning import FallbackPlanner, TripPlan, TripRequest, build_request
from transit_mcp.planning.external import (
    EngineError,
    HttpPlanner,
    ProcessPlanner,
    PythonPlanner,
)
from transit_mcp.planning.heuristic import HeuristicPlanner

ROOT = Path(__file__).resolve().parents[1]
PY_ENGINE = ROOT / "engines" / "example_python" / "engine.py"
JS_ENGINE = ROOT / "engines" / "example-js" / "engine.js"


@pytest.fixture
def request_obj(region_db, isolated_settings) -> TripRequest:
    return build_request(region_db, "Downtown A", "Office Park", day="weekday",
                         settings=isolated_settings)


def _py_cmd(code_or_path: str) -> str:
    return f'"{sys.executable}" {code_or_path}'


# -- contract -----------------------------------------------------------------
def test_request_is_pre_resolved(request_obj):
    assert request_obj.contract_version == "1"
    assert request_obj.origin.label.startswith("Downtown A")
    agencies = {s.agency for s in request_obj.origin.nearby_stops}
    assert {"mini-a", "wmata"} <= agencies
    assert request_obj.destination.nearby_stops[0].agency == "mini-b"
    # JSON round-trip: what an out-of-process engine receives
    again = TripRequest.model_validate(json.loads(request_obj.model_dump_json()))
    assert again == request_obj


def test_json_schemas_generate():
    for model in (TripRequest, TripPlan):
        schema = model.model_json_schema()
        assert schema["type"] == "object" and schema["properties"]


def test_heuristic_planner_returns_contract(region_db, request_obj):
    plan = HeuristicPlanner(region_db.path).plan(request_obj)
    assert plan.engine == "heuristic"
    opt = plan.options[0]
    assert [lg.mode for lg in opt.legs] == ["ride", "walk", "ride"]
    assert opt.total_fare == 5.5 and opt.feasible is True


# -- adapters -----------------------------------------------------------------
def _assert_example_plan(plan: TripPlan, engine: str):
    assert plan.engine == engine
    assert plan.options and plan.options[0].legs[0].agency in {"mini-a", "wmata"}
    assert "Example engine" in plan.warnings[0]


def test_python_planner_from_file(request_obj):
    planner = PythonPlanner(f"{PY_ENGINE}:create")
    assert planner.describe()["name"] == "example-python"
    _assert_example_plan(planner.plan(request_obj), "example-python")


def test_process_planner_python_example(request_obj):
    planner = ProcessPlanner(_py_cmd(f'"{PY_ENGINE}"'), timeout=15)
    try:
        assert planner.describe()["language"] == "python"
        _assert_example_plan(planner.plan(request_obj), "example-python")
        _assert_example_plan(planner.plan(request_obj), "example-python")  # stays warm
    finally:
        planner.close()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_process_planner_node_example(request_obj):
    planner = ProcessPlanner(f'node "{JS_ENGINE}"', timeout=15)
    try:
        assert planner.describe()["language"] == "javascript"
        _assert_example_plan(planner.plan(request_obj), "example-js")
    finally:
        planner.close()


def test_http_planner_against_stub(request_obj):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send({"name": "stub-http", "version": "9"})

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["req"] = req
            self._send({"origin": req["origin"]["label"],
                        "destination": req["destination"]["label"],
                        "options": [{"summary": "stub", "legs": []}]})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        planner = HttpPlanner(f"http://127.0.0.1:{server.server_port}", timeout=5)
        assert planner.describe()["name"] == "stub-http"
        plan = planner.plan(request_obj)
        assert plan.engine == "stub-http" and plan.options[0].summary == "stub"
        assert seen["req"]["context"]["feeds"] is not None
    finally:
        server.shutdown()


# -- failure handling ---------------------------------------------------------
@pytest.mark.parametrize("script, expect", [
    ("-c \"import sys; sys.exit(3)\"", "exited"),
    ("-c \"import time; time.sleep(30)\"", "did not answer"),
    ("-c \"import sys,json; [print(json.dumps({'id': json.loads(l)['id'], "
     "'result': {'nonsense': 1}}), flush=True) for l in sys.stdin]\"", "invalid TripPlan"),
    ("-c \"import sys,json; [print(json.dumps({'id': json.loads(l)['id'], "
     "'error': {'message': 'no graph loaded'}}), flush=True) for l in sys.stdin]\"",
     "no graph loaded"),
])
def test_broken_engines_raise_then_fall_back(region_db, request_obj, script, expect):
    broken = ProcessPlanner(_py_cmd(script), timeout=2)
    try:
        with pytest.raises(EngineError, match=expect):
            broken.plan(request_obj)
        plan = FallbackPlanner(broken, HeuristicPlanner(region_db.path)).plan(request_obj)
    finally:
        broken.close()
    assert plan.engine == "heuristic" and plan.options
    assert expect in plan.warnings[0] and "built-in" in plan.warnings[0]


def test_stray_stdout_lines_are_ignored(request_obj):
    chatty = (
        "-c \"import sys,json\nfor l in sys.stdin:\n"
        "    m=json.loads(l); print('loading graph...'); print('{not json');"
        " print(json.dumps({'id': -1, 'result': {}}));"
        " print(json.dumps({'id': m['id'], 'result': {'origin':'o','destination':'d'}}),"
        " flush=True)\""
    )
    planner = ProcessPlanner(_py_cmd(chatty), timeout=5)
    try:
        plan = planner.plan(request_obj)
    finally:
        planner.close()
    assert (plan.origin, plan.destination) == ("o", "d")


def test_misconfigured_engine_falls_back(region_db, isolated_settings):
    from transit_mcp.planning import get_planner, reset_planner_cache
    from transit_mcp.retrieval.connect import plan_connection

    settings = isolated_settings.model_copy(update={"planner_engine": "process",
                                                    "planner_command": None})
    reset_planner_cache()
    try:
        plan = plan_connection(region_db, "Downtown A", "Office Park", settings=settings)
        assert plan["engine"] == "heuristic"
        assert "PLANNER_COMMAND" in plan["warnings"][0]
        with pytest.raises(EngineError):
            get_planner(settings.model_copy(update={"db_path": region_db.path}),
                        fallback=False)
    finally:
        reset_planner_cache()
