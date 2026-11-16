"""Parking and park & ride: Metrorail's lots, from WMATA's API at every data refresh
(``wmata_parking``), and the lots curated in ``parking.toml`` from each operator's own
pages (MARC, VRE, Union Station, and the Reagan National, Dulles and BWI airports).

Each lot comes out as one dict with where it is, who runs it, spaces, cost, notes and
its source page; a curated MARC or VRE lot that is really a Metro lot (``same_as``) is
folded into that Metro station's entry rather than shown twice.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tomllib
from functools import lru_cache
from pathlib import Path

from transit_mcp.ingest.gtfs.clusters import haversine_m
from transit_mcp.planning.airports import load_links

log = logging.getLogger("transit_mcp.parking")

DEFAULT_FILE = Path(__file__).with_name("parking.toml")
WMATA_SOURCE = "https://www.wmata.com/ride/guides/parking.html"


@lru_cache(maxsize=4)
def curated(path: Path = DEFAULT_FILE) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _money(value) -> str | None:
    return None if value is None else f"${value:.2f}"


def _wmata_cost(r) -> str:
    rider, other = r["rider_cost"], r["nonrider_cost"]
    if not r["all_day_spaces"]:
        return "Short-term (Kiss & Ride) parking only"
    if rider is None:
        return "Daily parking; see WMATA for the rate"
    text = f"{_money(rider)} a day for Metro riders"
    if other is not None and abs(other - rider) > 0.005:
        text += f", {_money(other)} for non-riders"
    return text + " on weekdays; free for riders on weekends and federal holidays"


def _stop_point(db, spec: str) -> tuple[float, float] | None:
    """The middle of a feed's stops whose names start with a prefix ("marc:ODENTON MARC")."""
    feed, _, prefix = spec.partition(":")
    rows = db.conn.execute(
        "SELECT lat, lon FROM gtfs_stops WHERE feed_id = ? AND name LIKE ? AND lat IS NOT NULL",
        (feed, prefix.replace("%", "") + "%")).fetchall()
    if not rows:
        return None
    return (round(sum(r[0] for r in rows) / len(rows), 6),
            round(sum(r[1] for r in rows) / len(rows), 6))


def _wmata_lots(db, shared: dict[str, list[dict]]) -> list[dict]:
    try:
        rows = db.conn.execute(
            "SELECT p.*, s.name, s.lat, s.lon, s.address_json FROM wmata_parking p "
            "JOIN stations s ON s.code = p.code ORDER BY s.name").fetchall()
    except sqlite3.OperationalError:      # a database from before parking was kept
        return []
    lots = []
    for r in rows:
        address = json.loads(r["address_json"] or "{}")
        street = ", ".join(x for x in (address.get("Street"), address.get("City"),
                                       address.get("State")) if x)
        notes = [n for n in (r["notes"], r["short_term_notes"] and
                             f"Short-term parking: {r['short_term_notes']}") if n]
        also = []
        for other in shared.get(r["code"], []):
            also.append(other["operator"])
            if other.get("notes"):
                notes.append(other["notes"])
        lots.append({
            "id": f"wmata-{r['code']}", "operator": "Metro (WMATA)", "kind": "station",
            "name": f"{r['name']} Metro station", "lat": r["lat"], "lon": r["lon"],
            "available": True, "spaces": r["all_day_spaces"] or None,
            "short_term_spaces": r["short_term_spaces"] or None, "cost": _wmata_cost(r),
            "address": street or None, "notes": " ".join(notes) or None,
            "also_serves": also, "rules": "wmata", "source": WMATA_SOURCE,
            "checked": (r["retrieved_at"] or "")[:10] or None,
        })
    return lots


def all_lots(db, path: Path = DEFAULT_FILE) -> list[dict]:
    """Every parking lot we know of (see the module docstring)."""
    data = curated(path)
    shared: dict[str, list[dict]] = {}
    for lot in data.get("lot", []):
        if lot.get("same_as", "").startswith("wmata:"):
            shared.setdefault(lot["same_as"].split(":", 1)[1], []).append(lot)
    lots = _wmata_lots(db, shared)
    terminals = {a.code: a.terminal for a in load_links()}
    for lot in data.get("lot", []):
        if lot.get("same_as"):
            continue
        if lot.get("airport"):
            point = terminals.get(lot["airport"])
        elif lot.get("at"):
            point = tuple(lot["at"])
        else:
            point = _stop_point(db, lot.get("stop", ""))
        if point is None:
            log.warning("parking: no location for %s", lot["id"])
            continue
        lots.append({
            "id": lot["id"], "operator": lot["operator"], "kind": lot.get("kind", "station"),
            "name": lot["name"], "lat": point[0], "lon": point[1],
            "available": lot.get("available", True), "spaces": lot.get("spaces"),
            "cost": lot.get("cost"), "address": lot.get("address"), "notes": lot.get("notes"),
            "options": lot.get("options") or None,
            "rules": lot["operator"].lower() if lot["operator"].lower() in data.get("rules", {})
            else None,
            "source": lot["source"], "checked": lot.get("checked"),
        })
    return lots


def rules(path: Path = DEFAULT_FILE) -> dict[str, dict]:
    """Operator-wide rules ("wmata", "marc", "vre"): text, source and date checked."""
    return curated(path).get("rules", {})


def near(db, lat: float, lon: float, radius_m: float = 3000, limit: int = 8) -> list[dict]:
    """Lots within ``radius_m`` of a point, nearest first, each with ``distance_m``."""
    found = []
    for lot in all_lots(db):
        d = haversine_m(lat, lon, lot["lat"], lot["lon"])
        if d <= radius_m:
            found.append({**lot, "distance_m": round(d)})
    found.sort(key=lambda x: x["distance_m"])
    return found[:limit]
