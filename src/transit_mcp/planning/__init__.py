"""Pluggable trip planning.

``get_planner()`` returns the engine selected by ``PLANNER_ENGINE``:

=============  ==============================================================
``heuristic``  built-in connection advisor (default)
``python``     your engine, imported in-process (``PLANNER_PYTHON``)
``process``    your engine as a child process, JSON over stdio (``PLANNER_COMMAND``)
``http``       your engine as a web service (``PLANNER_URL``)
=============  ==============================================================

Unless ``PLANNER_FALLBACK=false``, a failing engine never breaks the assistant: the
built-in heuristic answers instead and the plan carries a warning saying so.
"""

from __future__ import annotations

import threading

from transit_mcp.config import Settings, get_settings
from transit_mcp.planning.external import (
    EngineError,
    HttpPlanner,
    ProcessPlanner,
    PythonPlanner,
)
from transit_mcp.planning.heuristic import HeuristicPlanner
from transit_mcp.planning.models import CONTRACT_VERSION, TripPlan, TripRequest
from transit_mcp.planning.request import UnresolvedPlace, build_request

ENGINES = ("heuristic", "python", "process", "http")


class FallbackPlanner:
    """Try the primary engine; on any failure answer with the fallback engine."""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.name = getattr(primary, "name", "engine")

    def describe(self) -> dict:
        info = dict(self.primary.describe())
        info.setdefault("fallback", self.fallback.name)
        return info

    def plan(self, request: TripRequest) -> TripPlan:
        try:
            return self.primary.plan(request)
        except Exception as exc:  # noqa: BLE001
            plan = self.fallback.plan(request)
            plan.warnings.insert(
                0,
                f"The '{self.name}' scheduling engine was unavailable ({exc}); "
                "showing the built-in connection guidance instead.",
            )
            return plan


def _build(settings: Settings):
    engine = (settings.planner_engine or "heuristic").strip().lower()
    timeout = settings.planner_timeout_seconds
    if engine == "heuristic":
        return HeuristicPlanner(settings.db_path)
    if engine == "python":
        if not settings.planner_python:
            raise EngineError("PLANNER_ENGINE=python needs PLANNER_PYTHON=package.module:factory")
        return PythonPlanner(settings.planner_python, settings)
    if engine == "process":
        if not settings.planner_command:
            raise EngineError("PLANNER_ENGINE=process needs PLANNER_COMMAND (e.g. node engine.js)")
        return ProcessPlanner(settings.planner_command, timeout=timeout)
    if engine == "http":
        if not settings.planner_url:
            raise EngineError("PLANNER_ENGINE=http needs PLANNER_URL")
        return HttpPlanner(settings.planner_url, timeout=timeout)
    raise EngineError(f"unknown PLANNER_ENGINE {engine!r}; choose one of {', '.join(ENGINES)}")


_cache: dict[tuple, object] = {}
_cache_lock = threading.Lock()


def get_planner(settings: Settings | None = None, *, fallback: bool | None = None):
    """The configured engine (cached, so a process engine stays warm)."""
    settings = settings or get_settings()
    use_fallback = settings.planner_fallback if fallback is None else fallback
    key = (
        settings.planner_engine, settings.planner_python, settings.planner_command,
        settings.planner_url, str(settings.db_path), use_fallback,
    )
    with _cache_lock:
        if key not in _cache:
            heuristic = HeuristicPlanner(settings.db_path)
            try:
                primary = _build(settings)
            except Exception as exc:  # misconfiguration
                if not use_fallback:
                    raise
                primary = _Broken(settings.planner_engine, exc)
            if primary.name == "heuristic" or not use_fallback:
                _cache[key] = primary
            else:
                _cache[key] = FallbackPlanner(primary, heuristic)
        return _cache[key]


class _Broken:
    """Stand-in for an engine that could not even be constructed."""

    def __init__(self, name: str, exc: Exception):
        self.name = name
        self._exc = exc

    def describe(self) -> dict:
        raise EngineError(str(self._exc))

    def plan(self, request: TripRequest) -> TripPlan:
        raise EngineError(str(self._exc))


def reset_planner_cache() -> None:
    with _cache_lock:
        for planner in _cache.values():
            for p in (planner, getattr(planner, "primary", None)):
                if hasattr(p, "close"):
                    p.close()
        _cache.clear()


__all__ = [
    "CONTRACT_VERSION",
    "ENGINES",
    "EngineError",
    "FallbackPlanner",
    "TripPlan",
    "TripRequest",
    "UnresolvedPlace",
    "build_request",
    "get_planner",
    "reset_planner_cache",
]
