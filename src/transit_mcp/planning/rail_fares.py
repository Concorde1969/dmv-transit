"""Station-to-station rail fares from the agencies' charts (``rail_fares.toml``).

VRE and MARC price trips by station, and their GTFS feeds don't carry those prices, so a
base fare can't describe a trip on them. Stations here are matched to GTFS stop names.
"""

from __future__ import annotations

import re
import tomllib
from functools import lru_cache
from pathlib import Path

DEFAULT_FILE = Path(__file__).with_name("rail_fares.toml")

# MARC's GTFS stop names -> the names on its fare charts (both as _key leaves them).
_MARC_NAMES = {
    "PENN STATION": "BALTO PENN STATION",
    "CAMDEN STATION": "BALTIMORE",
    "UNION STATION": "WASHINGTON DC",
    "MARTIN AIRPORT": "MARTIN STATE AIRPORT",
    "BWI RAIL STATION": "BWI AIRPORT",
    "LAUREL RACETRACK": "LAUREL PARK",
}


def _key(name: str) -> str:
    """"BRUNSWICK MD MARC eb" or "St. Denis" -> "BRUNSWICK", "ST DENIS"."""
    text = re.sub(r"\bMARC\b.*$", "", name.upper())
    text = re.sub(r"\b(W VA|MD)\s*$", "", text.strip())
    return " ".join(re.sub(r"[^A-Z ]", " ", text).split())


@lru_cache(maxsize=2)
def tables(path: Path = DEFAULT_FILE) -> dict:
    data = tomllib.loads(path.read_text("utf-8"))
    for line in data["marc"].values():
        line["index"] = {_key(s): i for i, s in enumerate(line["stations"])}
    return data


def _vre(board: str, alight: str) -> float | None:
    vre = tables()["vre"]
    a, b = vre["zones"].get(board), vre["zones"].get(alight)
    if a is None or b is None or board == alight:
        return None
    return vre["single"][max(a, b) - 1][min(a, b) - 1]


def _marc(line_name: str, board: str, alight: str) -> float | None:
    lines = tables()["marc"]
    line = next((lines[w] for w in line_name.lower().split() if w in lines), None)
    if line is None:
        return None
    a, b = _key(board), _key(alight)
    i, j = line["index"].get(_MARC_NAMES.get(a, a)), line["index"].get(_MARC_NAMES.get(b, b))
    if i is None or j is None:
        return None
    return line["fares"][i][j] or None


def leg_fare(feed: str, route_name: str | None, board: str | None,
             alight: str | None) -> float | None:
    """The full one-way fare for a ride, for the feeds priced by station. ``route_name``
    is the route's name, which says which MARC line it is."""
    if not board or not alight:
        return None
    if feed == "vre":
        return _vre(board, alight)
    if feed == "marc" and route_name:
        return _marc(route_name, board, alight)
    return None
