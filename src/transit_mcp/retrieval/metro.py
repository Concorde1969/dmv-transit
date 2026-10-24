"""Metrorail routing from the stored station order of each line.

Answers "how do I get from station A to station B by train": which line(s), where to
change trains, which direction ("toward <end station>"), and how many stops. Facts
come only from ``rail_line_stops`` (WMATA rail GTFS) and the ``stations``/``lines``/
``fares`` tables (WMATA API) — nothing is inferred.
"""

from __future__ import annotations

import heapq
import json

from transit_mcp.db.database import Database
from transit_mcp.retrieval.queries import is_stale, resolve_station
from transit_mcp.retrieval.search import search

TRANSFER_COST = 6  # a change of train "costs" as much as riding 6 extra stops


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
        "note": "Some trains end before the last station of a line — check the "
                "destination sign on the front of the train.",
        "source": "WMATA Metrorail line data (WMATA rail GTFS and WMATA API)",
        "url": "https://www.wmata.com/ride/maps.html",
        "retrieved_at": o["retrieved_at"],
        "stale": is_stale(o["retrieved_at"]),
    }
