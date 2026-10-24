"""Multi-agency getters: agencies, nearby stops, routes, transfers, how-to-pay.

These read the ``gtfs_*`` tables, the derived ``*_service_summary`` /
``stop_links`` tables, and the curated ``agency_profiles`` / ``fare_vending``.
Feasibility answers lean on ``stop_service_summary`` (which days / hours a stop
has service).
"""

from __future__ import annotations

import json
import math

from transit_mcp.config import get_settings
from transit_mcp.db.database import Database
from transit_mcp.retrieval.disruptions import TripScope, for_trip
from transit_mcp.retrieval.metro import station_service
from transit_mcp.retrieval.queries import (
    feed_provenance,
    is_stale,
    lookup_alias,
    resolve_station,
)

# Metrorail GTFS route_id -> the line code WMATA's alerts use.
_ROUTE_ALERT_ID = {("wmata", r): c for r, c in (
    ("RED", "RD"), ("BLUE", "BL"), ("GREEN", "GR"), ("YELLOW", "YL"),
    ("ORANGE", "OR"), ("SILVER", "SV"))}
# Agencies whose fare / payment pages are stored under another agency's slug.
_DOCS_FEED = {"metrobus": "wmata"}
_FARE_TEXT_CHARS = 2500

_DAY_ALIASES = {
    "mon": "mon", "monday": "mon", "tue": "tue", "tues": "tue", "tuesday": "tue",
    "wed": "wed", "weds": "wed", "wednesday": "wed", "thu": "thu", "thur": "thu",
    "thurs": "thu", "thursday": "thu", "fri": "fri", "friday": "fri",
    "sat": "sat", "saturday": "sat", "sun": "sun", "sunday": "sun",
    "weekday": "weekday", "weekend": "weekend", "today": "today",
}
_DAY_COLS = ("runs_mon", "runs_tue", "runs_wed", "runs_thu", "runs_fri",
             "runs_sat", "runs_sun")
_DAY_FULL = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
             "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _walk_min(dist_m: float) -> int:
    return max(1, round(dist_m / get_settings().walk_speed_m_per_min))


# ---------------------------------------------------------------------------
# Point resolution
# ---------------------------------------------------------------------------
def resolve_point(db: Database, place: str) -> dict | None:
    """Resolve a free-text place to a coordinate + a representative node.

    Accepts ``"lat,lon"``, a Metro station name/code, or a GTFS stop name.
    Returns ``{lat, lon, label, kind, feed, id}`` or ``None``.
    """
    p = place.strip()
    # "lat,lon"
    if "," in p:
        try:
            lat_s, lon_s = p.split(",", 1)
            lat, lon = float(lat_s), float(lon_s)
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return {"lat": lat, "lon": lon, "label": f"{lat:.5f},{lon:.5f}",
                        "kind": "point", "feed": None, "id": None}
        except ValueError:
            pass

    # An alias for a non-WMATA stop ("Mason" -> a CUE stop, etc.)
    alias = lookup_alias(db, p)
    if alias and alias["feed_id"] != "wmata":
        row = db.conn.execute(
            "SELECT feed_id, stop_id, name, lat, lon FROM gtfs_stops "
            "WHERE feed_id = ? AND stop_id = ?",
            (alias["feed_id"], alias["stop_id"]),
        ).fetchone()
        if row and row["lat"] is not None:
            return {"lat": row["lat"], "lon": row["lon"],
                    "label": f"{row['name']} ({row['feed_id']})",
                    "kind": "gtfs", "feed": row["feed_id"], "id": row["stop_id"]}

    st = resolve_station(db, p)
    if st and st["lat"] is not None:
        return {"lat": st["lat"], "lon": st["lon"], "label": f"{st['name']} station",
                "kind": "wmata", "feed": "wmata", "id": st["code"]}

    row = db.conn.execute(
        """
        SELECT feed_id, stop_id, name, lat, lon
          FROM gtfs_stops
         WHERE lat IS NOT NULL AND name LIKE ?
           AND feed_id NOT IN (SELECT feed_id FROM gtfs_feeds WHERE index_stops = 0)
         ORDER BY LENGTH(name) LIMIT 1
        """,
        (f"%{p}%",),
    ).fetchone()
    if row:
        return {"lat": row["lat"], "lon": row["lon"],
                "label": f"{row['name']} ({row['feed_id']})",
                "kind": "gtfs", "feed": row["feed_id"], "id": row["stop_id"]}
    return None


def _day_filter(day: str | None):
    """Return a column name to test truthy, or None for no filter."""
    if not day:
        return None
    key = _DAY_ALIASES.get(day.strip().lower())
    if key in (None, "weekday", "weekend", "today"):
        # 'today' resolved by caller if desired; weekday/weekend handled specially
        return key
    return f"runs_{key}"


# ---------------------------------------------------------------------------
# Agencies
# ---------------------------------------------------------------------------
def _fare_headline(fare: dict | None) -> str | None:
    if not fare:
        return None
    if fare["base_fare"] == 0:
        return "Free — no fare"
    methods = json.loads(fare["payment_methods"] or "[]")
    primary = methods[0] if methods else "see fare page"
    if fare["base_fare"] is not None:
        return f"~${fare['base_fare']:.2f} base; pay with {primary.lower()}"
    return f"Varies; pay with {primary.lower()}"


def list_agencies(db: Database) -> list[dict]:
    profiles = {
        r["feed_id"]: dict(r)
        for r in db.conn.execute("SELECT * FROM agency_profiles")
    }
    fares = {
        r["feed_id"]: dict(r) for r in db.conn.execute("SELECT * FROM fare_vending")
    }
    feeds = {
        r["feed_id"]: dict(r) for r in db.conn.execute("SELECT * FROM gtfs_feeds")
    }

    out: list[dict] = []
    # WMATA first, then every agency that has a profile or a loaded feed.
    rest = sorted((set(profiles) | set(feeds)) - {"wmata"})
    for slug in ["wmata", *rest]:
        prof = profiles.get(slug, {})
        feed = feeds.get(slug, {})
        fare = fares.get(slug)
        out.append({
            "feed_id": slug,
            "display_name": prof.get("display_name") or feed.get("agency_name") or slug,
            "mode": feed.get("mode") or ("rail+bus" if slug == "wmata" else None),
            "role": prof.get("role"),
            "strengths": prof.get("strengths"),
            "weaknesses": prof.get("weaknesses"),
            "service_area": prof.get("service_area"),
            "frequency_tier": prof.get("frequency_tier"),
            "connects_to": json.loads(prof.get("connects_to") or "[]"),
            "fare_headline": _fare_headline(fare),
            "fare_page": (fare or {}).get("fare_page_url"),
            "status": feed.get("status") or ("api" if slug == "wmata" else "not loaded"),
            "feed_fetched_at": feed.get("fetched_at"),
            "feed_valid_until": feed.get("feed_end_date"),
        })
    return out


# ---------------------------------------------------------------------------
# Nearby stops
# ---------------------------------------------------------------------------
def _stop_service(db: Database, feed: str, stop_id: str) -> dict | None:
    r = db.conn.execute(
        "SELECT * FROM stop_service_summary WHERE feed_id=? AND stop_id=?",
        (feed, stop_id),
    ).fetchone()
    return dict(r) if r else None


def _service_line(svc: dict | None) -> str:
    if not svc:
        return "service days unknown"
    days = [d[5:] for d, c in zip(_DAY_COLS, [svc[c] for c in _DAY_COLS]) if c]
    label = "every day" if len(days) == 7 else (", ".join(days) or "no regular service")
    span = ""
    if svc.get("weekday_first"):
        span = f"; weekdays {svc['weekday_first']}–{svc['weekday_last']}"
    if svc.get("saturday_first"):
        span += f"; Sat {svc['saturday_first']}–{svc['saturday_last']}"
    if svc.get("sunday_first"):
        span += f"; Sun {svc['sunday_first']}–{svc['sunday_last']}"
    return f"runs {label}{span}"


def find_stops_near(
    db: Database, place: str, *, radius_m: int | None = None, agency: str | None = None
) -> dict:
    pt = resolve_point(db, place)
    if not pt:
        return {"error": "could_not_resolve_place", "place": place}
    radius_m = radius_m or get_settings().nearby_search_radius_m
    # ~1 degree lat = 111 km; pad the bounding box.
    dlat = radius_m / 111_000
    dlon = radius_m / (111_000 * max(math.cos(math.radians(pt["lat"])), 0.1))

    results: list[dict] = []

    gtfs_rows = db.conn.execute(
        """
        SELECT feed_id, stop_id, name, lat, lon
          FROM gtfs_stops
         WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
           AND (location_type IS NULL OR location_type IN (0, ''))
           AND feed_id NOT IN (SELECT feed_id FROM gtfs_feeds WHERE index_stops = 0)
        """,
        (pt["lat"] - dlat, pt["lat"] + dlat, pt["lon"] - dlon, pt["lon"] + dlon),
    ).fetchall()
    for r in gtfs_rows:
        if agency and r["feed_id"] != agency:
            continue
        d = _haversine_m(pt["lat"], pt["lon"], r["lat"], r["lon"])
        if d > radius_m:
            continue
        svc = _stop_service(db, r["feed_id"], r["stop_id"])
        results.append({
            "agency": r["feed_id"], "kind": "gtfs", "stop_id": r["stop_id"],
            "name": r["name"], "lat": r["lat"], "lon": r["lon"],
            "distance_m": round(d), "walk_min": _walk_min(d),
            "routes": json.loads(svc["route_ids"]) if svc else [],
            "service": _service_line(svc),
            "serves_weekend": bool(svc["serves_weekend"]) if svc else None,
            "evening_service": bool(svc["evening_service"]) if svc else None,
        })

    if not agency or agency == "wmata":
        for r in db.conn.execute(
            """
            SELECT code, name, lat, lon, line_codes FROM stations
             WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
            """,
            (pt["lat"] - dlat, pt["lat"] + dlat, pt["lon"] - dlon, pt["lon"] + dlon),
        ):
            d = _haversine_m(pt["lat"], pt["lon"], r["lat"], r["lon"])
            if d > radius_m:
                continue
            results.append({
                "agency": "wmata", "kind": "wmata", "stop_id": r["code"],
                "name": f"{r['name']} Metro station", "lat": r["lat"], "lon": r["lon"],
                "distance_m": round(d),
                "walk_min": _walk_min(d),
                "routes": json.loads(r["line_codes"] or "[]"),
                "service": "Metrorail runs daily ~5am–midnight (later Fri/Sat)",
                "serves_weekend": True, "evening_service": True,
            })

    results.sort(key=lambda x: x["distance_m"])
    return {
        "resolved": pt["label"],
        "radius_m": radius_m,
        "count": len(results),
        "stops": results[:40],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _resolve_feed(db: Database, agency: str) -> str | None:
    a = agency.strip().lower()
    row = db.conn.execute(
        "SELECT feed_id FROM gtfs_feeds WHERE feed_id = ?", (a,)
    ).fetchone()
    if row:
        return row["feed_id"]
    row = db.conn.execute(
        "SELECT feed_id FROM agency_profiles WHERE LOWER(display_name) LIKE ? "
        "OR feed_id = ?",
        (f"%{a}%", a),
    ).fetchone()
    return row["feed_id"] if row else (a if a == "wmata" else None)


def get_route(db: Database, agency: str, query: str) -> dict:
    feed = _resolve_feed(db, agency)
    if not feed:
        return {"error": "unknown_agency", "agency": agency,
                "known": [a["feed_id"] for a in list_agencies(db)]}
    q = query.strip()
    row = db.conn.execute(
        """
        SELECT r.*, s.service_days, s.first_departure, s.last_departure,
               s.typical_headway_min, s.num_stops, s.first_stop_name,
               s.last_stop_name, s.runs_weekday, s.runs_saturday, s.runs_sunday
          FROM gtfs_routes r
          LEFT JOIN route_service_summary s
            ON s.feed_id = r.feed_id AND s.route_id = r.route_id
         WHERE r.feed_id = ?
           AND (r.route_id = ? OR r.short_name = ? OR r.short_name LIKE ?
                OR r.long_name LIKE ?)
         ORDER BY LENGTH(COALESCE(r.long_name, r.short_name)) LIMIT 1
        """,
        (feed, q, q, f"%{q}%", f"%{q}%"),
    ).fetchone()
    if not row:
        return {"error": "route_not_found", "agency": feed, "query": query}

    fare = db.conn.execute(
        "SELECT * FROM fare_vending WHERE feed_id=?", (feed,)
    ).fetchone()
    scope = TripScope()
    scope.add(feed, _ROUTE_ALERT_ID.get((feed, row["route_id"]), row["route_id"]))
    return {
        "agency": feed,
        "route": " ".join(x for x in (row["short_name"], row["long_name"]) if x),
        "description": row["description"],
        "runs": row["service_days"] or "see schedule",
        "service_span": (
            f"{row['first_departure']}–{row['last_departure']}"
            if row["first_departure"] else None
        ),
        "typical_headway_min": row["typical_headway_min"],
        "num_stops": row["num_stops"],
        "endpoints": [row["first_stop_name"], row["last_stop_name"]],
        "weekend": {
            "saturday": bool(row["runs_saturday"]),
            "sunday": bool(row["runs_sunday"]),
        },
        "route_url": row["route_url"],
        "fare": dict(fare) if fare else None,
        **for_trip(db, scope),
        **feed_provenance(db, feed),
    }


# ---------------------------------------------------------------------------
# Transfers at a point
# ---------------------------------------------------------------------------
def _nearest_node(db: Database, pt: dict) -> tuple[str, str, str] | None:
    """(kind, feed, id) of the closest stop/station to a resolved point."""
    if pt["kind"] in ("wmata", "gtfs"):
        return pt["kind"], pt["feed"], pt["id"]
    near = find_stops_near(db, f"{pt['lat']},{pt['lon']}", radius_m=500)
    stops = near.get("stops") or []
    if not stops:
        return None
    s = stops[0]
    return s["kind"], s["agency"], s["stop_id"]


def _feasible_on(svc: dict | None, day: str | None, time: str | None) -> tuple[bool, str]:
    if svc is None:
        return True, "service days unknown at this stop — verify the schedule"
    key = _day_filter(day)
    days_on = {d[5:] for d in _DAY_COLS if svc[d]}
    if key == "weekend":
        ok = bool({"sat", "sun"} & days_on)
        return ok, ("runs on weekends" if ok else "no weekend service here")
    if key == "weekday":
        ok = bool({"mon", "tue", "wed", "thu", "fri"} & days_on)
        return ok, ("runs on weekdays" if ok else "no weekday service here")
    if key and key.startswith("runs_"):
        ok = bool(svc[key])
        d = key[5:]
        full = _DAY_FULL.get(d, d.title())
        if not ok:
            return False, f"no {full} service at this stop"
        window = None
        if d in ("sat",):
            window = (svc["saturday_first"], svc["saturday_last"])
        elif d in ("sun",):
            window = (svc["sunday_first"], svc["sunday_last"])
        else:
            window = (svc["weekday_first"], svc["weekday_last"])
        if time and window and window[0]:
            t = time if len(time) == 8 else time + ":00"
            if not (window[0] <= t <= window[1]):
                return False, (
                    f"{full} service runs {window[0]}–{window[1]}; "
                    f"{time} is outside that window"
                )
            return True, f"{full} {window[0]}–{window[1]} covers {time}"
        return True, f"runs {full}" + (
            f" {window[0]}–{window[1]}" if window and window[0] else ""
        )
    return True, "runs; check the schedule for exact times"


def transfers_at(
    db: Database, place: str, *, day: str | None = None, time: str | None = None
) -> dict:
    pt = resolve_point(db, place)
    if not pt:
        return {"error": "could_not_resolve_place", "place": place}
    node = _nearest_node(db, pt)
    if not node:
        return {"error": "no_stop_near_place", "place": place, "resolved": pt["label"]}
    kind, feed, sid = node

    links = db.conn.execute(
        """
        SELECT * FROM stop_links
         WHERE (a_feed = ? AND a_id = ?) OR (b_feed = ? AND b_id = ?)
        """,
        (feed, sid, feed, sid),
    ).fetchall()

    by_agency: dict[str, list[dict]] = {}
    for lk in links:
        if lk["a_feed"] == feed and lk["a_id"] == sid:
            of, oi, on, ok = lk["b_feed"], lk["b_id"], lk["b_name"], lk["b_kind"]
        else:
            of, oi, on, ok = lk["a_feed"], lk["a_id"], lk["a_name"], lk["a_kind"]
        if of == feed:
            continue
        svc = station_service(db, oi) if ok == "wmata" else _stop_service(db, of, oi)
        feasible, why = _feasible_on(svc, day, time)
        entry = {
            "agency": of,
            "stop": on,
            "walk_min": lk["walk_min"],
            "distance_m": lk["distance_m"],
            "routes": (json.loads(_line_codes(db, oi)) if ok == "wmata" else
                       (json.loads(svc["route_ids"]) if svc else [])),
            "service": _service_line(svc) if svc else "schedule not loaded",
            "link_type": lk["link_source"],
            "overlap_days": lk["overlap_days"],
        }
        if day or time:
            entry["feasible"] = feasible
            entry["feasibility"] = why
        by_agency.setdefault(of, []).append(entry)

    for lst in by_agency.values():
        lst.sort(key=lambda x: x["walk_min"])

    return {
        "at": pt["label"],
        "matched_stop": {"kind": kind, "agency": feed, "id": sid},
        "day": day, "time": time,
        "connections": by_agency,
        "note": "Walking transfers based on stop proximity; service days and times "
        "from each agency's GTFS schedule. Confirm exact departure times before "
        "travelling.",
    }


def _line_codes(db: Database, code: str) -> str:
    r = db.conn.execute(
        "SELECT line_codes FROM stations WHERE code = ?", (code,)
    ).fetchone()
    return (r["line_codes"] if r else None) or "[]"


# ---------------------------------------------------------------------------
# How to pay
# ---------------------------------------------------------------------------
def how_to_pay(db: Database, agency: str) -> dict:
    feed = _resolve_feed(db, agency)
    if not feed:
        return {"error": "unknown_agency", "agency": agency,
                "known": [a["feed_id"] for a in list_agencies(db)]}
    fare = db.conn.execute(
        "SELECT * FROM fare_vending WHERE feed_id=?", (feed,)
    ).fetchone()
    prof = db.conn.execute(
        "SELECT display_name FROM agency_profiles WHERE feed_id=?", (feed,)
    ).fetchone()
    docs = db.conn.execute(
        """
        SELECT url, title, retrieved_at, body_markdown, category FROM documents
         WHERE feed_id = ? AND category IN ('fares', 'payment')
         ORDER BY category, LENGTH(url)
        """,
        (_DOCS_FEED.get(feed, feed),),
    ).fetchall()
    fare_doc = next((d for d in docs if d["category"] == "fares"), None)
    page_text = None
    if fare_doc:
        body = fare_doc["body_markdown"] or ""
        page_text = {
            "url": fare_doc["url"], "retrieved_at": fare_doc["retrieved_at"],
            "text": body[:_FARE_TEXT_CHARS] + (" ..." if len(body) > _FARE_TEXT_CHARS
                                               else ""),
        }
    docs = [{k: d[k] for k in ("url", "title", "retrieved_at")} for d in docs]
    if not fare:
        return {"agency": feed, "error": "no_fare_vending_data",
                "fare_page_text": page_text, "sources": docs}
    return {
        "agency": feed,
        "display_name": prof["display_name"] if prof else feed,
        # The agency's fare page, as published: the source for prices and rules.
        "fare_page_text": page_text,
        "summary_note": "The fields below are a hand-maintained summary. If they "
                        "differ from fare_page_text, the fare page is correct.",
        "base_fare": fare["base_fare"],
        "reduced_fare": fare["reduced_fare"],
        "currency": fare["currency"],
        "payment_methods": json.loads(fare["payment_methods"] or "[]"),
        "where_to_buy": json.loads(fare["where_to_buy"] or "[]"),
        "mobile_app": fare["mobile_app_name"],
        "accepts_smartrip": bool(fare["accepts_smartrip"]),
        "accepts_contactless": bool(fare["accepts_contactless"]),
        "cash_onboard": bool(fare["cash_onboard"]),
        "exact_change_required": bool(fare["exact_change_required"]),
        "transfer_policy": fare["transfer_policy"],
        "notes": fare["notes"],
        "fare_page": fare["fare_page_url"],
        "stale": is_stale(fare["updated_at"]),
        "sources": docs,
    }
