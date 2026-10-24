"""Adapters that let an engine you wrote yourself plan trips.

All three adapters speak the same contract (:mod:`transit_mcp.planning.models`):

* :class:`PythonPlanner` — ``PLANNER_PYTHON="package.module:factory"``. Imported and
  called in-process. ``factory(settings)`` returns an object with ``plan(request)``
  (returning a ``TripPlan`` or an equivalent dict) and optionally ``describe()``.
* :class:`ProcessPlanner` — ``PLANNER_COMMAND="node engines/my-engine/engine.js"``.
  A long-lived child process exchanging one JSON object per line on stdin/stdout:

      -> {"id": 1, "method": "describe", "params": {}}
      <- {"id": 1, "result": {"name": "my-engine", "version": "1.0"}}
      -> {"id": 2, "method": "plan", "params": <TripRequest>}
      <- {"id": 2, "result": <TripPlan>}          or  {"id": 2, "error": {"message": "..."}}

  Anything the engine prints that is not a JSON object with a matching ``id`` is
  ignored, and stderr is captured for diagnostics, so ``console.log`` debugging is
  safe as long as it goes to stderr (``console.error``).
* :class:`HttpPlanner` — ``PLANNER_URL="http://127.0.0.1:9000"``: ``POST /plan`` with a
  TripRequest body returns a TripPlan; ``GET /describe`` returns engine info.
"""

from __future__ import annotations

import collections
import importlib
import importlib.util
import itertools
import json
import os
import queue
import shlex
import subprocess
import threading
import time
from typing import Any

import httpx

from transit_mcp.planning.models import TripPlan, TripRequest


class EngineError(RuntimeError):
    """The engine failed, timed out, or returned something off-contract."""


def _as_plan(result: Any, engine_name: str) -> TripPlan:
    if isinstance(result, TripPlan):
        plan = result
    else:
        try:
            plan = TripPlan.model_validate(result)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"engine returned an invalid TripPlan: {exc}") from exc
    if plan.engine in ("", "external"):
        plan.engine = engine_name
    return plan


# ---------------------------------------------------------------------------
def _load_target(target: str):
    """Resolve 'package.module:attr' or 'path/to/file.py:attr' to the attribute."""
    module_ref, sep, attr = target.rpartition(":")
    if not sep or not module_ref or not attr or (len(module_ref) == 1 and module_ref.isalpha()):
        raise EngineError(
            "PLANNER_PYTHON must look like 'package.module:factory' or "
            f"'path/to/engine.py:factory', got {target!r}"
        )
    if module_ref.endswith(".py") or "/" in module_ref or "\\" in module_ref:
        path = os.path.abspath(module_ref)
        spec = importlib.util.spec_from_file_location(
            f"transit_mcp_engine_{abs(hash(path))}", path
        )
        if spec is None or spec.loader is None:
            raise EngineError(f"cannot load engine file {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_ref)
    return getattr(module, attr), module_ref


class PythonPlanner:
    def __init__(self, target: str, settings=None):
        factory, module_ref = _load_target(target)
        self._engine = factory(settings) if callable(factory) else factory
        self.name = getattr(self._engine, "name", None) or module_ref

    def describe(self) -> dict:
        fn = getattr(self._engine, "describe", None)
        return fn() if fn else {"name": self.name, "kind": "python"}

    def plan(self, request: TripRequest) -> TripPlan:
        return _as_plan(self._engine.plan(request), self.name)


# ---------------------------------------------------------------------------
class ProcessPlanner:
    def __init__(self, command: str, *, timeout: float = 20.0, cwd: str | None = None):
        self.command = command
        self.timeout = timeout
        self.cwd = cwd
        self.name = "process"
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(maxlen=50)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # -- process lifecycle -------------------------------------------------
    def _argv(self):
        # On Windows CreateProcess parses the command line itself.
        return self.command if os.name == "nt" else shlex.split(self.command)

    def _start(self) -> None:
        self._lines = queue.Queue()
        self._proc = subprocess.Popen(
            self._argv(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, cwd=self.cwd,
        )
        proc, lines, errbuf = self._proc, self._lines, self._stderr

        def pump_out():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)  # EOF: process exited

        def pump_err():
            for line in proc.stderr:
                errbuf.append(line.rstrip())

        threading.Thread(target=pump_out, daemon=True).start()
        threading.Thread(target=pump_err, daemon=True).start()

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                self._proc.kill()
        self._proc = None

    def _stderr_tail(self) -> str:
        tail = " | ".join(list(self._stderr)[-3:])
        return f" (engine stderr: {tail})" if tail else ""

    # -- request/response --------------------------------------------------
    def call(self, method: str, params: dict) -> Any:
        with self._lock:
            if not self._alive():
                self._start()
            msg_id = next(self._ids)
            try:
                self._proc.stdin.write(
                    json.dumps({"id": msg_id, "method": method, "params": params}) + "\n"
                )
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self.close()
                raise EngineError(
                    f"engine process is not accepting input{self._stderr_tail()}"
                ) from exc

            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()  # restart cleanly on the next call
                    raise EngineError(
                        f"engine did not answer '{method}' within {self.timeout:.0f}s"
                        f"{self._stderr_tail()}"
                    )
                try:
                    line = self._lines.get(timeout=remaining)
                except queue.Empty:
                    continue
                if line is None:
                    self.close()
                    raise EngineError(f"engine process exited{self._stderr_tail()}")
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue  # stray output, not a reply
                if not isinstance(reply, dict) or reply.get("id") != msg_id:
                    continue
                if "error" in reply and reply["error"] is not None:
                    err = reply["error"]
                    detail = err.get("message") if isinstance(err, dict) else str(err)
                    raise EngineError(f"engine reported an error: {detail}")
                return reply.get("result")

    def describe(self) -> dict:
        info = self.call("describe", {}) or {}
        self.name = info.get("name") or self.name
        return info

    def plan(self, request: TripRequest) -> TripPlan:
        return _as_plan(self.call("plan", request.model_dump(mode="json")), self.name)


# ---------------------------------------------------------------------------
class HttpPlanner:
    def __init__(self, base_url: str, *, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.name = "http"

    def describe(self) -> dict:
        try:
            r = httpx.get(f"{self.base_url}/describe", timeout=self.timeout)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise EngineError(f"engine at {self.base_url} is unreachable: {exc}") from exc
        info = r.json()
        self.name = info.get("name") or self.name
        return info

    def plan(self, request: TripRequest) -> TripPlan:
        try:
            r = httpx.post(
                f"{self.base_url}/plan", json=request.model_dump(mode="json"),
                timeout=self.timeout,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise EngineError(f"engine at {self.base_url} failed: {exc}") from exc
        return _as_plan(r.json(), self.name)
