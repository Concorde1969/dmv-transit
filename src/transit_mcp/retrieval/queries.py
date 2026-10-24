"""Typed getters over the structured tables, plus a freshness helper."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from transit_mcp.config import get_settings
from transit_mcp.db.database import Database
from transit_mcp.retrieval.search import search

GUIDE_TOPICS: dict[str, str] = {
    "new_rider": "new_rider",
    "new-rider": "new_rider",
    "visitor": "visitor",
    "payment": "payment",
    "pay": "payment",
    "fares": "fares",
    "fare": "fares",
    "accessibility": "accessibility",
    "access": "accessibility",
    "safety": "safety",
    "maps": "maps",
}


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------
def is_stale(retrieved_at: str | None) -> bool:
    if not retrieved_at:
        return True
    try:
        ts = datetime.fromisoformat(retrieved_at)
    except ValueError:
        return True
    return datetime.now() - ts > timedelta(hours=get_settings().stale_after_hours)


# ---------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------
def resolve_station(db: Database, name_or_code: str) -> sqlite3.Row | None:
    token = name_or_code.strip()
    row = db.conn.execute(
        "SELECT * FROM stations WHERE code = ?", (token.upper(),)
    ).fetchone()
    if row:
        return row
    rows = db.conn.execute(
        "SELECT * FROM stations WHERE name LIKE ? ORDER BY LENGTH(name) LIMIT 1",
        (f"%{token}%",),
    ).fetchall()
    return rows[0] if rows else None


def get_station_info(db: Database, name_or_code: str) -> dict | None:
    st = resolve_station(db, name_or_code)
    if not st:
        return None
    line_codes = json.loads(st["line_codes"] or "[]")
    lines = db.conn.execute(
        f"SELECT code, display_name, color_hex FROM lines "
        f"WHERE code IN ({','.join('?' for _ in line_codes) or 'NULL'})",
        line_codes,
    ).fetchall()
    doc = db.conn.execute(
        "SELECT body_markdown, url, retrieved_at FROM documents WHERE url = ?",
        (f"wmata://station/{st['code']}",),
    ).fetchone()
    return {
        "code": st["code"],
        "name": st["name"],
        "lines": [dict(r) for r in lines] or [{"code": c} for c in line_codes],
        "address": json.loads(st["address_json"] or "{}"),
        "has_parking": bool(st["has_parking"]),
        "transfer_with": json.loads(st["together_codes"] or "[]"),
        "lat": st["lat"],
        "lon": st["lon"],
        "details_markdown": doc["body_markdown"] if doc else None,
        "retrieved_at": st["retrieved_at"],
        "stale": is_stale(st["retrieved_at"]),
    }


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def get_service_alerts(
    db: Database,
    *,
    line: str | None = None,
    station: str | None = None,
    mode: str | None = None,
) -> list[dict]:
    sql = "SELECT * FROM alerts WHERE resolved_at IS NULL"
    params: list[object] = []
    if mode:
        sql += " AND mode = ?"
        params.append(mode)
    rows = db.conn.execute(sql + " ORDER BY updated_at DESC", params).fetchall()

    station_code = None
    if station:
        st = resolve_station(db, station)
        station_code = st["code"] if st else station.upper()

    out: list[dict] = []
    for r in rows:
        lines = json.loads(r["affected_lines"] or "[]")
        stations = json.loads(r["affected_stations"] or "[]")
        if line and line.upper() not in [x.upper() for x in lines]:
            continue
        if station_code and station_code not in stations:
            continue
        out.append(
            {
                "id": r["provider_alert_id"],
                "mode": r["mode"],
                "headline": r["headline"],
                "description": r["description"],
                "affected_lines": lines,
                "affected_stations": stations,
                "severity": r["severity"],
                "updated_at": r["updated_at"],
                "stale": is_stale(r["seen_at"]),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Elevator / escalator
# ---------------------------------------------------------------------------
def get_unit_status(
    db: Database, *, station: str | None = None, only_out: bool = True
) -> list[dict]:
    sql = "SELECT * FROM unit_status WHERE cleared_at IS NULL"
    params: list[object] = []
    if only_out:
        sql += " AND out_of_service = 1"
    if station:
        st = resolve_station(db, station)
        sql += " AND station_code = ?"
        params.append(st["code"] if st else station.upper())
    rows = db.conn.execute(sql + " ORDER BY station_code, unit_name", params).fetchall()
    return [
        {
            "station_code": r["station_code"],
            "unit_type": r["unit_type"],
            "unit_name": r["unit_name"],
            "location": r["location_description"],
            "symptom": r["symptom"],
            "out_of_service_since": r["date_out_of_service"],
            "estimated_return": r["estimated_return"],
            "updated_at": r["updated_at"],
            "stale": is_stale(r["seen_at"]),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Fares
# ---------------------------------------------------------------------------
_RIDER_COLUMN = {
    "regular": "peak_fare",
    "peak": "peak_fare",
    "offpeak": "offpeak_fare",
    "off-peak": "offpeak_fare",
    "senior": "senior_disabled_fare",
    "disabled": "senior_disabled_fare",
    "reduced": "senior_disabled_fare",
}


def get_fare(
    db: Database, origin: str, destination: str, *, rider_type: str = "regular"
) -> dict:
    src = resolve_station(db, origin)
    dst = resolve_station(db, destination)
    if not src or not dst:
        return {
            "error": "station_not_found",
            "origin_resolved": src["name"] if src else None,
            "destination_resolved": dst["name"] if dst else None,
        }
    row = db.conn.execute(
        "SELECT * FROM fares WHERE origin_code = ? AND dest_code = ?",
        (src["code"], dst["code"]),
    ).fetchone()
    if not row:
        row = db.conn.execute(
            "SELECT * FROM fares WHERE origin_code = ? AND dest_code = ?",
            (dst["code"], src["code"]),
        ).fetchone()
    context = [h.as_dict() for h in search(db, f"{origin} {destination} fare", limit=2)]
    if not row:
        return {
            "origin": src["name"],
            "destination": dst["name"],
            "fare": None,
            "note": "No station-to-station fare on file; see context for fare policy.",
            "context": context,
        }
    return {
        "origin": src["name"],
        "destination": dst["name"],
        "peak_fare": row["peak_fare"],
        "offpeak_fare": row["offpeak_fare"],
        "senior_disabled_fare": row["senior_disabled_fare"],
        "requested": {
            "rider_type": rider_type,
            "fare": row[_RIDER_COLUMN.get(rider_type.lower(), "peak_fare")],
        },
        "est_rail_time_min": row["rail_time_min"],
        "retrieved_at": row["retrieved_at"],
        "stale": is_stale(row["retrieved_at"]),
        "context": context,
    }


# ---------------------------------------------------------------------------
# Topics / guides / freshness
# ---------------------------------------------------------------------------
def list_topics(db: Database) -> list[dict]:
    rows = db.conn.execute(
        """
        SELECT category,
               COUNT(*)        AS documents,
               MIN(retrieved_at) AS oldest,
               MAX(retrieved_at) AS newest
          FROM documents
         GROUP BY category
         ORDER BY category
        """
    ).fetchall()
    return [
        {**dict(r), "stale": is_stale(r["newest"])}
        for r in rows
    ]


def get_guide(db: Database, topic: str) -> dict | None:
    category = GUIDE_TOPICS.get(topic.strip().lower())
    if not category:
        return {
            "error": "unknown_topic",
            "known_topics": sorted(set(GUIDE_TOPICS.values())),
        }
    row = db.conn.execute(
        """
        SELECT title, url, body_markdown, retrieved_at
          FROM documents
         WHERE category = ?
         ORDER BY LENGTH(body_markdown) DESC
         LIMIT 1
        """,
        (category,),
    ).fetchone()
    if not row:
        return None
    others = db.conn.execute(
        "SELECT title, url FROM documents WHERE category = ? AND url != ? "
        "ORDER BY LENGTH(body_markdown) DESC LIMIT 8",
        (category, row["url"]),
    ).fetchall()
    return {
        "topic": category,
        "title": row["title"],
        "url": row["url"],
        "markdown": row["body_markdown"],
        "retrieved_at": row["retrieved_at"],
        "stale": is_stale(row["retrieved_at"]),
        "related": [dict(r) for r in others],
    }


def freshness_report(db: Database) -> dict:
    base = db.freshness()
    base["counts"] = db.table_counts()
    base["stale_after_hours"] = get_settings().stale_after_hours
    return base
