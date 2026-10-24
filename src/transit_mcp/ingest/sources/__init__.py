"""Registry of ingest source plugins.

Built-in plugins: ``gtfs``, ``web_pages``, ``wmata_api``. Add your own either by
decorating a class in any imported module::

    from transit_mcp.ingest.sources import register_source

    @register_source
    class MyAgencyApi:
        type = "my_agency_api"
        scope = "api"
        updates_network = True
        def run(self, ctx, agency, source): ...

or, from a separate package, by declaring an entry point in its pyproject.toml::

    [project.entry-points."transit_mcp.sources"]
    my_agency_api = "my_package.sources:MyAgencyApi"
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points

from transit_mcp.ingest.sources.base import IngestContext, RunOptions, Scope, Source

_REGISTRY: dict[str, Source] = {}
_BUILTINS = (
    "transit_mcp.ingest.sources.gtfs",
    "transit_mcp.ingest.sources.web_pages",
    "transit_mcp.ingest.sources.wmata_api",
)
_loaded = False


def register_source(cls):
    """Class decorator: instantiate and register a source plugin by its ``type``."""
    inst = cls() if isinstance(cls, type) else cls
    if not isinstance(inst, Source):
        raise TypeError(f"{cls!r} does not implement the Source protocol")
    _REGISTRY[inst.type] = inst
    return cls


def _load_all() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for mod in _BUILTINS:
        import_module(mod)
    for ep in entry_points(group="transit_mcp.sources"):
        register_source(ep.load())


def get_source(source_type: str) -> Source | None:
    _load_all()
    return _REGISTRY.get(source_type)


def available_sources() -> dict[str, Source]:
    _load_all()
    return dict(_REGISTRY)


__all__ = [
    "IngestContext",
    "RunOptions",
    "Scope",
    "Source",
    "available_sources",
    "get_source",
    "register_source",
]
