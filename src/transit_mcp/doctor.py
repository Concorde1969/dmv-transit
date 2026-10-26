"""Health checks with plain-English results (``transit-mcp doctor``, ``launch``)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from transit_mcp.config import Settings, get_settings

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str  # OK | WARN | FAIL
    message: str
    fix: str | None = None


def _db_facts(db_path: Path) -> dict | None:
    if not db_path.is_file():
        return None
    from transit_mcp.db.database import Database

    with Database(db_path) as db:
        def one(sql: str):
            try:
                return db.conn.execute(sql).fetchone()[0]
            except Exception:  # noqa: BLE001 - table may not exist yet
                return None

        return {
            "stations": one("SELECT COUNT(*) FROM stations") or 0,
            "feeds_loaded": one("SELECT COUNT(*) FROM gtfs_feeds WHERE status='loaded'") or 0,
            "documents": one("SELECT COUNT(*) FROM documents") or 0,
            "via_transitland": one(
                "SELECT COUNT(*) FROM gtfs_feeds WHERE status='loaded' "
                "AND fetched_via LIKE 'transitland%'"
            ) or 0,
            "last_ingest": one(
                "SELECT MAX(finished_at) FROM ingest_runs WHERE status IN ('ok','partial')"
            ),
        }


def has_data(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    facts = _db_facts(Path(settings.db_path))
    return bool(facts and (facts["stations"] or facts["feeds_loaded"] or facts["documents"]))


def run_checks(settings: Settings | None = None, *, check_planner: bool = True) -> list[Check]:
    settings = settings or get_settings()
    checks: list[Check] = []

    ok_py = sys.version_info >= (3, 11)
    checks.append(Check(
        "Python", OK if ok_py else FAIL, f"Python {sys.version.split()[0]}",
        None if ok_py else "Install Python 3.11 or newer from https://www.python.org/",
    ))

    env_file = Path(".env")
    if env_file.is_file():
        checks.append(Check("Settings file", OK, ".env found"))
    elif settings.anthropic_api_key:
        checks.append(Check("Settings file", OK, "using environment variables"))
    else:
        checks.append(Check(
            "Settings file", WARN, ".env not found in this folder",
            "Copy .env.example to .env and fill in your keys.",
        ))
    if settings.is_production:
        checks.append(Check(
            "Mode", OK,
            f"production (limits: {settings.chat_rate_per_minute}/min and "
            f"{settings.chat_rate_per_day}/day per visitor, {settings.chat_daily_limit}/day "
            "site-wide)",
        ))

    checks.append(Check(
        "Anthropic key", OK if settings.anthropic_api_key else FAIL,
        "set" if settings.anthropic_api_key else "ANTHROPIC_API_KEY is empty — the chat "
        "assistant cannot answer without it",
        None if settings.anthropic_api_key else
        "Get a key at https://console.anthropic.com/ and put it in .env",
    ))
    checks.append(Check(
        "WMATA key", OK if settings.wmata_api_key else WARN,
        "set" if settings.wmata_api_key else "WMATA_API_KEY is empty — live Metro alerts, "
        "elevator status and station fares will be missing",
        None if settings.wmata_api_key else
        "Get a free key at https://developer.wmata.com/ and put it in .env",
    ))

    # Agencies + source plugins
    from transit_mcp.agencies import discover
    from transit_mcp.ingest.sources import available_sources

    agencies, errors = discover(settings)
    checks.append(Check(
        "Agency definitions", FAIL if errors else OK,
        "; ".join(errors) if errors else f"{len(agencies)} agencies: "
        + ", ".join(a.slug for a in agencies),
        "Fix the file(s) named above (see agencies/_template.toml)." if errors else None,
    ))
    plugins = available_sources()
    unknown = sorted({
        f"{a.slug}:{s.type}" for a in agencies for s in a.sources if s.type not in plugins
    })
    checks.append(Check(
        "Source plugins", FAIL if unknown else OK,
        f"unknown source types: {', '.join(unknown)}" if unknown
        else "available: " + ", ".join(sorted(plugins)),
        "Install/register the plugin or fix the `type` in the agency file." if unknown else None,
    ))

    pending = [
        a.slug for a in agencies for s in a.sources_of("gtfs")
        if s.options().get("manual")
        and not (Path(settings.feeds_dir) / a.slug / "google_transit.zip").is_file()
    ]
    if pending:
        checks.append(Check(
            "Manual feeds", WARN,
            f"waiting for a downloaded feed: {', '.join(pending)}",
            "See feeds/README.md — save each as feeds/<agency>/google_transit.zip",
        ))

    # Database
    facts = _db_facts(Path(settings.db_path))
    if facts is None:
        checks.append(Check(
            "Transit data", WARN, f"no database yet at {settings.db_path}",
            "It will be created and filled the first time you run start.bat.",
        ))
    elif not (facts["stations"] or facts["feeds_loaded"] or facts["documents"]):
        checks.append(Check(
            "Transit data", WARN, "database is empty",
            "Run refresh-data.bat (or `transit-mcp ingest all`).",
        ))
    else:
        age_note, status = "", OK
        if facts["last_ingest"]:
            try:
                age = datetime.now() - datetime.fromisoformat(facts["last_ingest"])
                age_note = f"; last refreshed {facts['last_ingest'][:16]}"
                if age > timedelta(days=7):
                    status = WARN
            except ValueError:
                pass
        checks.append(Check(
            "Transit data", status,
            f"{facts['stations']} Metro stations, {facts['feeds_loaded']} schedule feeds "
            f"({facts['via_transitland']} via Transitland), "
            f"{facts['documents']} documents{age_note}",
            "Run refresh-data.bat to get current alerts and schedules."
            if status == WARN else None,
        ))

    # Map trip planner
    missing = [name for name, value in (("GEOCODE_API_KEY", settings.geocode_api_key),
                                        ("TRANSITLAND_API_KEY", settings.transitland_api_key))
               if not value]
    checks.append(Check(
        "Map planner keys", WARN if missing else OK,
        f"{', '.join(missing)} empty — "
        + ("typed addresses can't be found" if "GEOCODE_API_KEY" in missing else
           "live times only for agencies with their own realtime feed")
        if missing else "set",
        "Put the missing key(s) in .env (see .env.example)." if missing else None,
    ))
    if facts is not None:
        from transit_mcp.api_budget import ApiBudget

        try:
            budget = ApiBudget.transitland(None, settings, shared=True)
            used = budget.used()
            checks.append(Check(
                "Transitland calls", WARN if budget.remaining() <= 0 else OK,
                f"{used:,} of {budget.limit:,} used this month"
                + (" — live times paused until next month" if budget.remaining() <= 0
                   else ""),
            ))
        except Exception:  # noqa: BLE001 - older database without the table
            pass
    map_file = Path(settings.map_tiles_path)
    checks.append(Check(
        "Map", OK if map_file.is_file() else WARN,
        f"{map_file.stat().st_size / 1e6:,.0f} MB basemap" if map_file.is_file()
        else f"no basemap at {map_file} — the map shows a plain background",
        None if map_file.is_file() else
        "Run deploy/map/fetch-basemap.ps1 (PC) or deploy/map/fetch-basemap.sh (server).",
    ))

    if check_planner:
        from transit_mcp.planning import get_planner

        engine = settings.planner_engine
        try:
            info = get_planner(settings, fallback=False).describe()
            checks.append(Check(
                "Trip planner", OK,
                f"{engine} engine ready ({info.get('name', engine)} {info.get('version', '')})"
                .strip(),
            ))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(
                "Trip planner", WARN if settings.planner_fallback else FAIL,
                f"{engine} engine not available: {exc}"
                + (" — the built-in planner will answer instead"
                   if settings.planner_fallback else ""),
                "Check PLANNER_* settings in .env (see docs/EXTENDING.md).",
            ))

    return checks


def format_checks(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"  [{c.status:^4}] {c.name}: {c.message}")
        if c.fix and c.status != OK:
            lines.append(f"         -> {c.fix}")
    return "\n".join(lines)
