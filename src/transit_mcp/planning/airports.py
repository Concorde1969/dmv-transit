"""Airport links: the step between a terminal and its rail station (``airports.toml``).

BWI's rail station is a shuttle ride from the terminal; DCA's and IAD's are a walk
away. GTFS feeds don't describe either, so without this a trip to "BWI airport" never
reaches MARC or Amtrak, and riders get no directions inside the airport.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from transit_mcp.ingest.gtfs.clusters import haversine_m

DEFAULT_FILE = Path(__file__).with_name("airports.toml")


@dataclass(frozen=True)
class AirportLink:
    code: str
    name: str
    terminal: tuple[float, float]
    area_m: int
    station: tuple[float, float]
    station_name: str
    station_m: int
    mode: str                         # "shuttle" or "walkway"
    to_station: str
    from_station: str
    source: str
    minutes: int | None = None
    every_min: int | None = None
    night_every_min: int | None = None
    night: tuple[int, int] | None = None   # seconds since midnight

    def at_airport(self, lat: float, lon: float) -> bool:
        return haversine_m(lat, lon, *self.terminal) <= self.area_m

    def wait_s(self, now_s: int) -> int:
        """Expected wait for the shuttle: half the time between shuttles."""
        every = self.every_min or 0
        if self.night and self.night_every_min:
            t = now_s % 86_400
            if self.night[0] <= t < self.night[1]:
                every = self.night_every_min
        return every * 30

    def info(self, end: str) -> dict:
        """What the trip step shows. ``end``: "origin" (terminal -> station) or
        "destination" (station -> terminal)."""
        return {"code": self.code, "name": self.name, "mode": self.mode,
                "station": self.station_name,
                "text": self.to_station if end == "origin" else self.from_station,
                "source": self.source}


def _clock(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 3600 + int(m) * 60


@lru_cache(maxsize=4)
def load_links(path: Path = DEFAULT_FILE) -> tuple[AirportLink, ...]:
    data = tomllib.loads(path.read_text("utf-8"))
    out = []
    for a in data.get("airport", []):
        if a["mode"] not in ("shuttle", "walkway"):
            raise ValueError(f"{path}: {a['code']}: mode must be shuttle or walkway")
        night = a.get("night")
        out.append(AirportLink(
            code=a["code"], name=a["name"], terminal=tuple(a["terminal"]),
            area_m=int(a["area_m"]), station=tuple(a["station"]),
            station_name=a["station_name"], station_m=int(a["station_m"]),
            mode=a["mode"], to_station=" ".join(a["to_station"].split()),
            from_station=" ".join(a["from_station"].split()), source=a["source"],
            minutes=a.get("minutes"), every_min=a.get("every_min"),
            night_every_min=a.get("night_every_min"),
            night=(_clock(night[0]), _clock(night[1])) if night else None))
    return tuple(out)
