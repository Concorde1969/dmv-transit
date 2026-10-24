"""Metrorail routing from the stored station order of each line.

Answers "how do I get from station A to station B by train": which line(s), where to
change trains, which direction ("toward <end station>"), how many stops, and on which
days / between which times scheduled trains make each leg. Routes and schedules come
from WMATA's rail GTFS (``rail_line_stops`` + the ``gtfs_*`` tables, fetched via
Transitland); station names and fares from the WMATA API — nothing is inferred.
"""

from __future__ import annotations

import heapq
import json

from transit_mcp.db.database import Database
from transit_mcp.ingest.gtfs.summarize import _service_day_masks
from transit_mcp.ingest.wmata_api import _CODE_RE
from transit_mcp.retrieval.disruptions import TripScope, for_trip
from transit_mcp.retrieval.queries import (
    feed_provenance,
    resolve_station,
    wmata_fare_check,
)
from transit_mcp.retrieval.search import search

TRANSFER_COST = 6  # a change of train "costs" as much as riding 6 extra stops
RAIL_FEED = "wmata"  # the Metrorail GTFS is loaded under the wmata agency slug
_LINE_ROUTE = {"RD": "RED", "BL": "BLUE", "GR": "GREEN", "YL": "YELLOW",
               "OR": "ORANGE", "SV": "SILVER"}
_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _network(db: Database):
    lines: dict[str, list[str]] = {}
    for r in db.conn.execute(
        "SELECT line_code, station_code FROM rail_line_stops ORDER BY line_code, seq"
    ):
        lines.setdefault(r["line_code"], []).append(r["station_code"])
    stations = {
        r["code"]: dict(r)
        for r in db.conn.execute(
            "SELECT code, name, together_codes, retrieved_at FROM stations"
        )
    }
    line_names = {
        r["code"]: r["display_name"]
        for r in db.conn.execute("SELECT code, display_name FROM lines")
    }
    # Two-level stations (Metro Center A01/C01, L'Enfant D03/F03, ...) are one place.
    complex_of: dict[str, frozenset[str]] = {}
    for code, st in stations.items():
        complex_of[code] = frozenset({code, *json.loads(st["together_codes"] or "[]")})
    return lines, stations, line_names, complex_of


def _leg(lines, stations, line_names, line, i, j) -> dict:
    seq = lines[line]
    toward = seq[-1] if j > i else seq[0]
    return {
        "line": line,
        "line_name": line_names.get(line, line),
        "board": stations[seq[i]]["name"],
        "board_code": seq[i],
        "alight": stations[seq[j]]["name"],
        "alight_code": seq[j],
        "stops": abs(j - i),
        "toward": stations[toward]["name"],
    }


def route_between_codes(db: Database, origin_code: str, dest_code: str) -> dict | None:
    """Legs between two station codes, or None if line data isn't loaded / no path."""
    lines, stations, line_names, complex_of = _network(db)
    if not lines or origin_code not in stations or dest_code not in stations:
        return None
    o_set = complex_of.get(origin_code, frozenset({origin_code}))
    d_set = complex_of.get(dest_code, frozenset({dest_code}))
    if o_set & d_set:
        return {"legs": [], "transfers": 0}

    # 1. A direct ride is always preferred for first-time riders.
    direct = []
    for line, seq in lines.items():
        i = next((k for k, c in enumerate(seq) if c in o_set), None)
        j = next((k for k, c in enumerate(seq) if c in d_set), None)
        if i is not None and j is not None:
            direct.append((abs(j - i), line, i, j))
    if direct:
        _, line, i, j = min(direct)
        return {"legs": [_leg(lines, stations, line_names, line, i, j)], "transfers": 0}

    # 2. Otherwise: fewest stops, with each change of train weighted heavily.
    at_station: dict[str, list[tuple[str, int]]] = {}
    for line, seq in lines.items():
        for k, code in enumerate(seq):
            for c in complex_of.get(code, frozenset({code})):
                at_station.setdefault(c, []).append((line, k))

    dist: dict[tuple[str, int], int] = {}
    prev: dict[tuple[str, int], tuple[str, int] | None] = {}
    heap: list[tuple[int, str, int]] = []
    for line, k in at_station.get(origin_code, []):
        dist[(line, k)] = 0
        prev[(line, k)] = None
        heapq.heappush(heap, (0, line, k))
    goal = None
    while heap:
        cost, line, k = heapq.heappop(heap)
        if cost > dist.get((line, k), 1 << 30):
            continue
        if lines[line][k] in d_set:
            goal = (line, k)
            break
        moves = [((line, k + d), 1) for d in (-1, 1) if 0 <= k + d < len(lines[line])]
        moves += [((l2, k2), TRANSFER_COST)
                  for l2, k2 in at_station.get(lines[line][k], []) if l2 != line]
        for node, step in moves:
            nc = cost + step
            if nc < dist.get(node, 1 << 30):
                dist[node], prev[node] = nc, (line, k)
                heapq.heappush(heap, (nc, *node))
    if goal is None:
        return None

    path = []
    node = goal
    while node is not None:
        path.append(node)
        node = prev[node]
    path.reverse()

    legs, start = [], 0
    for idx in range(1, len(path) + 1):
        if idx == len(path) or path[idx][0] != path[start][0]:
            line = path[start][0]
            i, j = path[start][1], path[idx - 1][1]
            if i != j:
                legs.append(_leg(lines, stations, line_names, line, i, j))
            start = idx
    return {"legs": legs, "transfers": max(0, len(legs) - 1)}


def _platforms(db: Database, codes: frozenset[str]) -> list[str]:
    """GTFS stop_ids (platforms) whose parent station is one of these station codes
    (WMATA parents look like ``STN_C10`` or ``STN_A01_C01``)."""
    if not codes:
        return []
    likes = " OR ".join("parent_station LIKE ?" for _ in codes)
    return [
        r["stop_id"]
        for r in db.conn.execute(
            f"SELECT stop_id, parent_station FROM gtfs_stops WHERE feed_id = ? AND ({likes})",
            (RAIL_FEED, *(f"%{c}%" for c in codes)),
        )
        if codes & set(_CODE_RE.findall(r["parent_station"]))
    ]


def _clock(t: str) -> str:
    """GTFS 'HH:MM:SS' (hours may pass 24) -> '5:04 am' / '12:30 am (after midnight)'."""
    h, m = (int(x) for x in t.split(":")[:2])
    late = h >= 24
    h %= 24
    label = f"{(h % 12) or 12}:{m:02d} {'am' if h < 12 else 'pm'}"
    return label + (" (after midnight)" if late else "")


def leg_service(db: Database, leg: dict, complex_of: dict) -> dict | None:
    """Days and first/last departures of scheduled trains on ``leg['line']`` from the
    board station to the alight station, from the Metrorail GTFS. None if the rail
    GTFS isn't loaded; ``{"days": []}`` if no scheduled train makes the trip."""
    route_id = _LINE_ROUTE.get(leg["line"])
    if not route_id or not db.conn.execute(
        "SELECT 1 FROM gtfs_trips WHERE feed_id = ? LIMIT 1", (RAIL_FEED,)
    ).fetchone():
        return None
    board = _platforms(db, complex_of.get(leg["board_code"], frozenset({leg["board_code"]})))
    alight = _platforms(db, complex_of.get(leg["alight_code"],
                                           frozenset({leg["alight_code"]})))
    if not board or not alight:
        return {"days": []}
    cur = db.conn.cursor()
    cur.row_factory = None
    bm, am = ",".join("?" * len(board)), ",".join("?" * len(alight))
    # departures at the board station on this line: trip -> (sequence, time, service)
    departs = {
        trip: (seq, dep, service)
        for trip, seq, dep, service in cur.execute(
            f"""
            SELECT st.trip_id, st.stop_sequence, st.departure_time, t.service_id
              FROM gtfs_stop_times st
              JOIN gtfs_trips t ON t.feed_id = st.feed_id AND t.trip_id = st.trip_id
             WHERE st.feed_id = ? AND st.stop_id IN ({bm}) AND t.route_id = ?
            """,
            (RAIL_FEED, *board, route_id),
        )
    }
    by_service: dict[str, list[str]] = {}
    for trip, seq in cur.execute(
        f"SELECT trip_id, stop_sequence FROM gtfs_stop_times "
        f"WHERE feed_id = ? AND stop_id IN ({am})", (RAIL_FEED, *alight),
    ):
        hit = departs.get(trip)
        if hit and seq > hit[0] and hit[1]:  # the train reaches the alight station
            by_service.setdefault(hit[2], []).append(hit[1])
    masks = _service_day_masks(db, RAIL_FEED)
    by_day: dict[int, dict] = {}
    for service, deps in by_service.items():
        first, last = min(deps, key=_secs), max(deps, key=_secs)
        for dow, runs in enumerate(masks.get(service, [0] * 7)):
            if not runs:
                continue
            day = by_day.setdefault(dow, {"first": first, "last": last})
            day["first"] = min(day["first"], first, key=_secs)
            day["last"] = max(day["last"], last, key=_secs)
    # Group days that share the same first/last times: "Mon–Fri 5:02 am – 11:58 pm".
    groups: list[tuple[list[int], dict]] = []
    for dow in sorted(by_day):
        if groups and groups[-1][1] == by_day[dow] and groups[-1][0][-1] == dow - 1:
            groups[-1][0].append(dow)
        else:
            groups.append(([dow], by_day[dow]))
    return {
        "days": [_DAY_KEYS[d] for d in sorted(by_day)],
        "first_and_last_trains": [
            {"days": _span(days), "first_departure": _clock(t["first"]),
             "last_departure": _clock(t["last"])}
            for days, t in groups
        ],
    }


def station_service(db: Database, code: str) -> dict | None:
    """When trains leave a station (any line), from the Metrorail GTFS, shaped like a
    ``stop_service_summary`` row: ``runs_mon..runs_sun`` plus first/last departure
    for weekdays, Saturday and Sunday. None if the rail GTFS isn't loaded."""
    if not db.conn.execute(
        "SELECT 1 FROM gtfs_trips WHERE feed_id = ? LIMIT 1", (RAIL_FEED,)
    ).fetchone():
        return None
    complex_of = _network(db)[3]
    platforms = _platforms(db, complex_of.get(code, frozenset({code})))
    if not platforms:
        return None
    marks = ",".join("?" * len(platforms))
    rows = db.conn.execute(
        f"""
        SELECT t.service_id, MIN(st.departure_time) AS first,
               MAX(st.departure_time) AS last
          FROM gtfs_stop_times st
          JOIN gtfs_trips t ON t.feed_id = st.feed_id AND t.trip_id = st.trip_id
         WHERE st.feed_id = ? AND st.stop_id IN ({marks})
         GROUP BY t.service_id
        """,
        (RAIL_FEED, *platforms),
    ).fetchall()
    masks = _service_day_masks(db, RAIL_FEED)
    out: dict = {f"runs_{d}": 0 for d in _DAY_KEYS}
    windows: dict[str, list[str]] = {}
    for r in rows:
        for dow, runs in enumerate(masks.get(r["service_id"], [0] * 7)):
            if not runs or not r["first"]:
                continue
            out[f"runs_{_DAY_KEYS[dow]}"] = 1
            group = "weekday" if dow < 5 else ("saturday" if dow == 5 else "sunday")
            windows.setdefault(group, []).extend([r["first"], r["last"]])
    for group in ("weekday", "saturday", "sunday"):
        times = windows.get(group)
        out[f"{group}_first"] = min(times, key=_secs) if times else None
        out[f"{group}_last"] = max(times, key=_secs) if times else None
    return out


def _secs(t: str) -> int:
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def _span(days: list[int]) -> str:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return names[days[0]] if len(days) == 1 else f"{names[days[0]]}–{names[days[-1]]}"


def metro_route(db: Database, origin: str, destination: str) -> dict:
    o = resolve_station(db, origin)
    d = resolve_station(db, destination)
    if not o or not d:
        return {
            "error": "station_not_found",
            "origin_resolved": o["name"] if o else None,
            "destination_resolved": d["name"] if d else None,
            "note": "Use a Metrorail station name (e.g. 'Rosslyn') or code (e.g. 'C05').",
        }
    if not db.conn.execute("SELECT 1 FROM rail_line_stops LIMIT 1").fetchone():
        return {"error": "line_data_not_loaded",
                "note": "Metrorail line data hasn't been loaded yet; run a full refresh."}
    route = route_between_codes(db, o["code"], d["code"])
    if route is None:
        return {"error": "no_route_found", "origin": o["name"], "destination": d["name"]}
    if not route["legs"]:
        return {"error": "same_station", "origin": o["name"], "destination": d["name"]}

    parts = []
    for n, leg in enumerate(route["legs"]):
        verb = "Take" if n == 0 else "Change to"
        parts.append(f"{verb} the {leg['line_name']} toward {leg['toward']} for "
                     f"{leg['stops']} stop{'s' if leg['stops'] != 1 else ''} to {leg['alight']}")
    changes = [
        f"At {prev['alight']}, get off and follow the signs for {leg['line_name']} "
        f"trains toward {leg['toward']}."
        for prev, leg in zip(route["legs"], route["legs"][1:])
    ]
    # WMATA's own fare-period wording (weekday / evening / weekend), so the model
    # never has to say from memory which fare applies when.
    fare_context = [
        h.as_dict() for h in search(db, "Metro Rail Fares weekday weekend", limit=4)
        if (h.url or "").startswith("https://www.wmata.com")
    ][:1]
    fare = db.conn.execute(
        "SELECT peak_fare, offpeak_fare, senior_disabled_fare, rail_time_min, retrieved_at "
        "FROM fares WHERE origin_code = ? AND dest_code = ?",
        (o["code"], d["code"]),
    ).fetchone()

    complex_of = _network(db)[3]
    service_note = None
    for leg in route["legs"]:
        leg["service"] = leg_service(db, leg, complex_of)
    if any(leg["service"] is None for leg in route["legs"]):
        service_note = ("Metrorail schedule data (GTFS) isn't loaded, so service days "
                        "and hours can't be confirmed here.")
    elif any(not leg["service"]["days"] for leg in route["legs"]):
        service_note = ("WMATA's schedule has no train for at least one of these legs "
                        "in the current timetable — check wmata.com before travelling.")
    prov = feed_provenance(db, RAIL_FEED)
    scope = TripScope()
    for leg in route["legs"]:
        codes = (complex_of.get(leg["board_code"], frozenset({leg["board_code"]}))
                 | complex_of.get(leg["alight_code"], frozenset({leg["alight_code"]})))
        scope.add("wmata", leg["line"], codes)
    return {
        "origin": o["name"],
        "destination": d["name"],
        "direct": route["transfers"] == 0,
        "transfers": route["transfers"],
        "change_trains_at": [leg["alight"] for leg in route["legs"][:-1]],
        "how_to_change_trains": changes,
        "legs": route["legs"],
        "summary": ". ".join(parts) + ".",
        "est_rail_time_min": fare["rail_time_min"] if fare else None,
        "fare": ({"peak": fare["peak_fare"], "off_peak": fare["offpeak_fare"],
                  "senior_disabled": fare["senior_disabled_fare"]} if fare else None),
        "fare_note": "One fare covers the whole Metrorail trip; changing trains is free. "
                     "Which fare applies depends on the day and time — see fare_context "
                     "(WMATA's fare page) rather than assuming.",
        "fare_context": fare_context,
        "fare_check": (wmata_fare_check(db, fare["peak_fare"], fare["offpeak_fare"],
                                        fare["senior_disabled_fare"]) if fare else None),
        "service_note": service_note,
        **for_trip(db, scope),
        "note": "Some trains end before the last station of a line — check the "
                "destination sign on the front of the train.",
        **prov,
    }
