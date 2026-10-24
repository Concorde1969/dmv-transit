"""``type = "wmata_api"`` — WMATA Developer API: incidents, stations, lines,
parking, entrances, and the station-to-station fare matrix.

TOML options::

    [[sources]]
    type = "wmata_api"
    include_fares = true   # ~100 extra calls; `ingest --no-fares` also skips them

Requires ``WMATA_API_KEY``. This is the template for any agency that has its own
real-time API rather than (or as well as) GTFS: write a plugin like this one.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from transit_mcp.agencies.models import AgencyConfig, SourceConfig
from transit_mcp.db.database import Database
from transit_mcp.ingest.chunker import chunk_markdown
from transit_mcp.ingest.sources import register_source
from transit_mcp.ingest.sources.base import IngestContext
from transit_mcp.ingest.wmata_api import (
    API_BASE,
    ENDPOINTS,
    WmataApiClient,
    normalize_bus_incidents,
    normalize_elevator_incidents,
    normalize_entrances,
    normalize_lines,
    normalize_rail_incidents,
    normalize_station_to_station,
    normalize_stations,
    parking_station_codes,
)
from transit_mcp.models import Document


class WmataApiOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_fares: bool = True


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _store_doc(db: Database, feed_id: str, *, url: str, category: str, title: str,
               summary: str, body: str) -> None:
    doc = Document(
        url=url, category=category, feed_id=feed_id, title=title, summary=summary,
        body_markdown=body, raw_hash=_hash(body), retrieved_at=datetime.now(),
    )
    sid, _ = db.upsert_source(
        url=url, source_type="api", category=category, feed_id=feed_id,
        http_status=200, content_hash=doc.raw_hash,
    )
    db.replace_document(
        source_id=sid, document=doc, chunks=chunk_markdown(body, doc_title=title)
    )


def _line_docs(db: Database, feed_id: str, lines_data: dict) -> int:
    n = 0
    for line in normalize_lines(lines_data):
        body = (
            f"# {line.display_name}\n\n"
            f"Line code: **{line.code}**\n\n"
            f"Terminal stations: {line.start_station_code or '?'} "
            f"to {line.end_station_code or '?'}.\n"
        )
        _store_doc(
            db, feed_id, url=f"wmata://line/{line.code}", category="line",
            title=f"{line.display_name} (Metrorail)",
            summary=f"{line.display_name}, code {line.code}.", body=body,
        )
        n += 1
    return n


def _station_docs(db: Database, feed_id: str, stations_data: dict,
                  entrances_by_station: dict, parking: set[str]) -> int:
    n = 0
    for st in normalize_stations(stations_data, parking):
        addr = st.address
        addr_line = ", ".join(
            v for v in (addr.get("Street"), addr.get("City"), addr.get("State"),
                        addr.get("Zip")) if v
        )
        parts = [
            f"# {st.name} station (Metrorail)",
            "",
            f"Station code: **{st.code}**",
            f"Lines served: {', '.join(st.line_codes) or 'unknown'}",
            f"Address: {addr_line or 'n/a'}",
            f"Parking available: {'yes' if st.has_parking else 'no'}",
        ]
        if st.together_codes:
            parts.append(
                f"Transfer station: shares a complex with {', '.join(st.together_codes)}."
            )
        entrances = entrances_by_station.get(st.code, [])
        if entrances:
            parts.append("\n## Entrances\n")
            parts += [
                f"- {e.get('description') or e.get('name') or 'Entrance'}"
                for e in entrances
            ]
        _store_doc(
            db, feed_id, url=f"wmata://station/{st.code}", category="station",
            title=f"{st.name} station (Metrorail)",
            summary=f"{st.name}: lines {', '.join(st.line_codes) or 'n/a'}; "
            f"parking {'yes' if st.has_parking else 'no'}.",
            body="\n".join(parts) + "\n",
        )
        n += 1
    return n


@register_source
class WmataApiSource:
    type = "wmata_api"
    scope = "api"
    updates_network = True  # writes the `stations` table used by stop_links

    def run(self, ctx: IngestContext, agency: AgencyConfig, source: SourceConfig) -> dict:
        opts = WmataApiOptions.model_validate(source.options())
        db, fid = ctx.db, agency.slug
        client = WmataApiClient(ctx.api_http, ctx.settings.require_api_key())
        stats: dict = {"errors": []}

        def record(endpoint_url: str, category: str, raw_text: str) -> int:
            sid, _ = db.upsert_source(
                url=endpoint_url, source_type="api", category=category, feed_id=fid,
                http_status=200, content_hash=_hash(raw_text),
            )
            return sid

        # --- lines ---
        raw, data = client.lines()
        sid = record(API_BASE + ENDPOINTS["lines"], "line", raw.text)
        for line in normalize_lines(data):
            db.upsert_line(source_id=sid, line=line)
        stats["lines"] = len(data.get("Lines", []) or [])
        stats["line_docs"] = _line_docs(db, fid, data)
        db.commit()

        # --- parking (flags stations) and entrances (station docs) ---
        try:
            _, parking_data = client.station_parking()
            parking = parking_station_codes(parking_data)
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"parking: {exc}")
            parking = set()
        try:
            raw_e, ent_data = client.station_entrances()
            record(API_BASE + ENDPOINTS["station_entrances"], "station", raw_e.text)
            entrances = normalize_entrances(ent_data)
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"entrances: {exc}")
            entrances = {}

        # --- stations ---
        raw_s, stations_data = client.stations()
        sid_s = record(API_BASE + ENDPOINTS["stations"], "station", raw_s.text)
        for st in normalize_stations(stations_data, parking):
            db.upsert_station(source_id=sid_s, station=st)
        stats["stations"] = len(stations_data.get("Stations", []) or [])
        stats["station_docs"] = _station_docs(db, fid, stations_data, entrances, parking)
        db.commit()

        # --- incidents: rail, bus, elevator/escalator ---
        def incidents(name, fetch, normalize, mode):
            try:
                raw_i, payload = fetch()
                sid_i = record(API_BASE + ENDPOINTS[name], "alerts", raw_i.text)
                alerts = normalize(payload)
                for a in alerts:
                    db.upsert_alert(source_id=sid_i, alert=a)
                db.reconcile_alerts(mode=mode, seen_ids=[a.provider_alert_id for a in alerts])
                db.commit()
                return len(alerts)
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"{name}: {exc}")
                return None

        stats["rail_alerts"] = incidents(
            "rail_incidents", client.rail_incidents, normalize_rail_incidents, "rail")
        stats["bus_alerts"] = incidents(
            "bus_incidents", client.bus_incidents, normalize_bus_incidents, "bus")

        try:
            raw_ee, ee = client.elevator_incidents()
            sid_ee = record(API_BASE + ENDPOINTS["elevator_incidents"], "alerts", raw_ee.text)
            units, unit_alerts = normalize_elevator_incidents(ee)
            for u in units:
                db.upsert_unit_status(source_id=sid_ee, unit=u)
            db.reconcile_unit_status(seen_keys=[(u.station_code, u.unit_name) for u in units])
            for a in unit_alerts:
                db.upsert_alert(source_id=sid_ee, alert=a)
            db.reconcile_alerts(
                mode="elevator_escalator", seen_ids=[a.provider_alert_id for a in unit_alerts]
            )
            stats["units_out"] = len(units)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"elevator_incidents: {exc}")

        # --- station-to-station fare matrix (one call per origin station) ---
        if opts.include_fares and ctx.options.include_fares:
            fare_rows = 0
            for (code,) in db.conn.execute("SELECT code FROM stations ORDER BY code").fetchall():
                try:
                    raw_f, fdata = client.station_to_station(code)
                except Exception as exc:  # noqa: BLE001
                    stats["errors"].append(f"fares {code}: {exc}")
                    continue
                sid_f = record(
                    f"{API_BASE}{ENDPOINTS['station_to_station']}?FromStationCode={code}",
                    "fares", raw_f.text,
                )
                for fare in normalize_station_to_station(fdata):
                    db.upsert_fare(source_id=sid_f, fare=fare)
                    fare_rows += 1
                db.commit()
            stats["fare_rows"] = fare_rows

        stats["errors"] = stats["errors"] or None
        return stats
