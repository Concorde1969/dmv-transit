"""Leave-now trip planner for the map: real stops, today's timetable, live departures.

Flow (the site owner's design):
1. Find the closest boardable stops of every agency near the start and the end (up to
   ``TRIP_STOPS_PER_AGENCY`` each, within ``TRIP_MAX_WALK_M``, about a mile); walking
   time to and from them counts toward each option's arrival time and is shown.
2. From today's GTFS timetable (local database), find **direct** trips — a trip that
   leaves a start stop within the next ``TRIP_WINDOW_MIN`` (3 hours) and later calls at
   an end stop.
3. Only when needed (no direct trip, or a much faster option exists), find **one-
   transfer** trips: ride to a stop, walk to another stop less than 0.3 km away (any
   agency), ride on. Only when no trip with fewer changes works, **two-transfer** trips
   (``TRIP_MAX_TRANSFERS``).
   At an airport (``airports.toml``) the step between the terminal and its rail station
   is added: BWI's free shuttle, DCA's and IAD's walkways, with the airport's directions.
4. Check **live departures** (Transitland, merged GTFS-realtime) for the boarding stops
   of the best options, on feeds that have realtime; delays carry forward along a trip.
5. Throw out connections that can't be made and vehicles that have already left.
   Options whose transfers are all short (``TRIP_SHORT_TRANSFER_MIN``, 10 min) come first;
   options with a longer wait at a transfer (up to ``TRIP_MAX_TRANSFER_WAIT_MIN``) are
   shown only when there is nothing better, so the rider can decide.

Times are seconds since local midnight of today's service day (trips belonging to
yesterday's service, e.g. 25:10:00, are shifted by −24 h). Results carry epoch
seconds for the browser's countdowns.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from transit_mcp.db.database import Database
from transit_mcp.ingest.gtfs.clusters import haversine_m
from transit_mcp.ingest.gtfs.shapes import decode
from transit_mcp.planning.airports import AirportLink, load_links

DAY = 86_400
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ROUTE_MODES = {0: "tram", 1: "subway", 2: "rail", 3: "bus", 4: "ferry", 5: "cable_tram",
               6: "aerial", 7: "funicular", 11: "trolleybus", 12: "monorail"}


# A stop time as zero-padded "HH:MM:SS" text (some feeds write "9:05:00"), so SQLite
# can compare it with the search window.
_DEP_TEXT = ("(CASE WHEN length(d) = 7 THEN '0' || d ELSE d END)"
             .replace("d", "COALESCE(NULLIF(st.departure_time, ''), st.arrival_time)"))


def hms(seconds: int) -> str:
    seconds = max(0, seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def tsec(t: str | None) -> int | None:
    """'HH:MM:SS' (hours may pass 24) -> seconds."""
    if not t:
        return None
    try:
        h, m, s = t.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except ValueError:
        return None


@dataclass
class Point:
    lat: float
    lon: float
    label: str = ""


@dataclass
class NearStop:
    feed: str
    stop_id: str
    name: str
    lat: float
    lon: float
    walk_s: int                   # time to or from the stop, including an airport shuttle
    link: AirportLink | None = None   # reached through an airport's shuttle or walkway
    link_s: int = 0               # the part of walk_s spent waiting for and on a shuttle


@dataclass
class Call:
    """One scheduled stop of one trip."""
    feed: str
    trip_id: str
    stop_id: str
    seq: int
    arr: int
    dep: int
    route_id: str
    headsign: str | None
    shape_id: str | None


@dataclass
class Ride:
    feed: str
    trip_id: str
    route_id: str
    headsign: str | None
    shape_id: str | None
    board_stop: str
    board_seq: int
    dep: int                      # scheduled, seconds since local midnight
    alight_stop: str
    alight_seq: int
    arr: int
    delay: int = 0                # live delay (s), carried along the trip
    live: bool = False
    cancelled: bool = False

    @property
    def dep_eff(self) -> int:
        return self.dep + self.delay

    @property
    def arr_eff(self) -> int:
        return self.arr + self.delay


@dataclass
class Candidate:
    rides: list[Ride]
    walk_start_s: int
    walk_end_s: int
    transfer_walk_s: list[int] = field(default_factory=list)

    @property
    def arrive(self) -> int:
        return self.rides[-1].arr_eff + self.walk_end_s

    def signature(self) -> tuple:
        return tuple((r.feed, r.route_id) for r in self.rides)


@dataclass
class LiveSettings:
    max_walk_m: int = 1600        # farthest start/end stop considered (straight line)
    stops_per_feed: int = 25      # closest stops of each agency at each end
    transfer_m: int = 300         # farthest walk between two vehicles (straight line)
    short_transfer_s: int = 600   # transfer waits up to this are preferred
    max_wait_s: int = 7200        # longest wait at a transfer considered at all
    min_transfer_s: int = 60
    window_s: int = 3600
    max_ride_s: int = 3 * 3600
    walk_m_per_min: float = 70.0
    detour: float = 1.25          # walking distance vs straight line
    max_options: int = 6
    departures_per_route: int = 4  # per route, direction and start stop
    transfer_only_if_faster_s: int = 300   # on top of the 5 min each transfer costs
    max_live_calls: int = 8
    max_transfers: int = 2         # two-transfer trips only when nothing simpler works
    two_transfer_wait_s: int = 3600   # longest wait at either change of such a trip

    @classmethod
    def from_settings(cls, s) -> LiveSettings:
        return cls(max_walk_m=s.trip_max_walk_m, stops_per_feed=s.trip_stops_per_agency,
                   short_transfer_s=s.trip_short_transfer_min * 60,
                   max_wait_s=s.trip_max_transfer_wait_min * 60,
                   window_s=s.trip_window_min * 60, walk_m_per_min=s.trip_walk_m_per_min,
                   max_live_calls=s.trip_max_live_calls,
                   max_transfers=s.trip_max_transfers)


class LivePlanner:
    def __init__(self, db: Database, *, cfg: LiveSettings | None = None,
                 realtime=None, agencies: dict | None = None,
                 tz: str = "America/New_York", now: datetime | None = None,
                 airports: tuple[AirportLink, ...] | None = None):
        self.db = db
        self.airports = load_links() if airports is None else airports
        self.cfg = cfg or LiveSettings()
        self.realtime = realtime          # planning.realtime.TransitlandRealtime or None
        self.agencies = agencies or {}    # slug -> {"name", "has_realtime", ...}
        self.tz = ZoneInfo(tz)
        self.now = now or datetime.now(self.tz)
        self.today = self.now.date()
        midnight = datetime.combine(self.today, datetime.min.time(), self.tz)
        self.now_s = int((self.now - midnight).total_seconds())
        self.midnight_epoch = int(midnight.timestamp())
        self._services: dict[tuple[str, date], set[str]] = {}
        self._stop_cache: dict[tuple[str, str], dict] = {}
        self._trip_calls: dict[tuple[str, str], list[Call]] = {}
        self._boardings: dict[str, list[Call]] = {}
        self._links: dict[str, dict[tuple[str, str], NearStop]] = {"origin": {},
                                                                    "destination": {}}
        self._down: list[tuple[Call, Call, int]] = []
        self._up: dict[tuple[str, str], list[tuple[Call, Call, int]]] = {}
        self.live_calls = 0

    # -- reference data ----------------------------------------------------------------
    def services(self, feed: str, d: date) -> set[str]:
        key = (feed, d)
        if key not in self._services:
            col, ds = _DAYS[d.weekday()], d.strftime("%Y%m%d")
            base = {r[0] for r in self.db.conn.execute(
                f"SELECT service_id FROM gtfs_calendar WHERE feed_id = ? AND {col} = 1 "
                "AND start_date <= ? AND end_date >= ?", (feed, ds, ds))}
            for r in self.db.conn.execute(
                "SELECT service_id, exception_type FROM gtfs_calendar_dates "
                "WHERE feed_id = ? AND date = ?", (feed, ds)):
                if int(r[1]) == 1:
                    base.add(r[0])
                else:
                    base.discard(r[0])
            self._services[key] = base
        return self._services[key]

    def stop(self, feed: str, stop_id: str) -> dict:
        key = (feed, stop_id)
        if key not in self._stop_cache:
            r = self.db.conn.execute(
                """
                SELECT s.stop_id, s.name, s.lat, s.lon, s.code, p.name AS parent_name
                  FROM gtfs_stops s
                  LEFT JOIN gtfs_stops p ON p.feed_id = s.feed_id
                                        AND p.stop_id = s.parent_station
                 WHERE s.feed_id = ? AND s.stop_id = ?
                """, (feed, stop_id)).fetchone()
            self._stop_cache[key] = dict(r) if r else {"stop_id": stop_id, "name": stop_id,
                                                       "lat": None, "lon": None}
        return self._stop_cache[key]

    def walk_s(self, meters: float) -> int:
        return int(math.ceil(meters * self.cfg.detour / self.cfg.walk_m_per_min * 60))

    def _stops_within(self, lat: float, lon: float, r: float):
        """Boardable stops within ``r`` meters: (row, distance)."""
        dlat = r / 111_320.0
        dlon = r / (111_320.0 * max(math.cos(math.radians(lat)), 0.1))
        for row in self.db.conn.execute(
            """
            SELECT s.feed_id, s.stop_id, s.name, s.lat, s.lon, par.name AS parent_name
              FROM gtfs_stops s
              LEFT JOIN gtfs_stops par ON par.feed_id = s.feed_id
                                      AND par.stop_id = s.parent_station
             WHERE s.lat BETWEEN ? AND ? AND s.lon BETWEEN ? AND ?
               AND (s.location_type IS NULL OR s.location_type IN (0, ''))
            """,
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ):
            d = haversine_m(lat, lon, row["lat"], row["lon"])
            if d <= r:
                yield row, d

    def stops_near(self, p: Point, end: str | None = None) -> list[NearStop]:
        """The closest stops of each agency, nearest first — plus, at an airport, its
        rail station's stops through the shuttle or walkway. ``end`` ("origin" or
        "destination") records those links for the trip description."""
        out = [NearStop(row["feed_id"], row["stop_id"], row["parent_name"] or row["name"],
                        row["lat"], row["lon"], self.walk_s(d))
               for row, d in self._stops_within(p.lat, p.lon, self.cfg.max_walk_m)]
        out.sort(key=lambda s: s.walk_s)
        kept: dict[str, int] = defaultdict(int)
        nearest = []
        for stop in out:
            if kept[stop.feed] < self.cfg.stops_per_feed:
                kept[stop.feed] += 1
                nearest.append(stop)
        linked = self._airport_stops(p)
        if end:
            self._links[end] = {(s.feed, s.stop_id): s for s in linked}
        if linked:   # the airport's own way to its station replaces a straight-line walk
            keys = {(s.feed, s.stop_id) for s in linked}
            nearest = sorted([s for s in nearest if (s.feed, s.stop_id) not in keys] + linked,
                             key=lambda s: s.walk_s)
        return nearest

    def _airport_stops(self, p: Point) -> list[NearStop]:
        out = []
        for link in self.airports:
            if not link.at_airport(p.lat, p.lon):
                continue
            for row, d in self._stops_within(*link.station, link.station_m):
                if link.mode == "shuttle":
                    shuttle = (link.minutes or 0) * 60 + link.wait_s(self.now_s)
                    walk = self.walk_s(d) + shuttle
                elif link.minutes:
                    walk, shuttle = link.minutes * 60, 0
                else:
                    walk, shuttle = self.walk_s(haversine_m(*link.terminal, row["lat"],
                                                            row["lon"])), 0
                out.append(NearStop(row["feed_id"], row["stop_id"],
                                    row["parent_name"] or row["name"], row["lat"], row["lon"],
                                    walk, link=link, link_s=shuttle))
        return out

    # -- timetable queries ---------------------------------------------------------------
    def calls_at(self, feed: str, stop_ids: list[str],
                 dep_range: tuple[int, int] | None = None) -> list[Call]:
        """Calls at these stops today (incl. yesterday's after-midnight trips), optionally
        only those departing within ``dep_range`` (seconds since midnight)."""
        if not stop_ids:
            return []
        today = self.services(feed, self.today)
        yday = self.services(feed, self.today - timedelta(days=1))
        out: list[Call] = []
        when, args = "", []
        if dep_range:   # today's times, or yesterday's after-midnight times (+24 h)
            lo, hi = dep_range
            when = f"AND ({_DEP_TEXT} BETWEEN ? AND ? OR {_DEP_TEXT} BETWEEN ? AND ?)"
            args = [hms(lo), hms(hi), hms(lo + DAY), hms(hi + DAY)]
        for i in range(0, len(stop_ids), 500):
            chunk = stop_ids[i:i + 500]
            for r in self.db.conn.execute(
                f"""
                SELECT st.trip_id, st.stop_id, st.stop_sequence, st.arrival_time,
                       st.departure_time, t.service_id, t.route_id, t.headsign, t.shape_id
                  FROM gtfs_stop_times st
                  JOIN gtfs_trips t ON t.feed_id = st.feed_id AND t.trip_id = st.trip_id
                 WHERE st.feed_id = ? AND st.stop_id IN ({','.join('?' * len(chunk))})
                       {when}
                """, (feed, *chunk, *args),
            ):
                out.extend(self._to_calls(feed, r, today, yday, dep_range))
        return out

    def calls_of_trips(self, feed: str, trip_ids: list[str]) -> dict[str, list[Call]]:
        """All calls of these trips, in stop order (service day already decided).
        Cached for the search: direct and transfer searches ask for the same trips."""
        missing = [t for t in dict.fromkeys(trip_ids) if (feed, t) not in self._trip_calls]
        for t in missing:
            self._trip_calls[(feed, t)] = []
        for tid, calls in self._fetch_trips(feed, missing).items():
            self._trip_calls[(feed, tid)] = calls
        return {t: self._trip_calls[(feed, t)] for t in trip_ids}

    def _fetch_trips(self, feed: str, trip_ids: list[str]) -> dict[str, list[Call]]:
        today = self.services(feed, self.today)
        yday = self.services(feed, self.today - timedelta(days=1))
        out: dict[str, list[Call]] = defaultdict(list)
        for i in range(0, len(trip_ids), 500):
            chunk = trip_ids[i:i + 500]
            for r in self.db.conn.execute(
                f"""
                SELECT st.trip_id, st.stop_id, st.stop_sequence, st.arrival_time,
                       st.departure_time, t.service_id, t.route_id, t.headsign, t.shape_id
                  FROM gtfs_stop_times st
                  JOIN gtfs_trips t ON t.feed_id = st.feed_id AND t.trip_id = st.trip_id
                 WHERE st.feed_id = ? AND st.trip_id IN ({','.join('?' * len(chunk))})
                """, (feed, *chunk),
            ):
                for c in self._to_calls(feed, r, today, yday):
                    out[c.trip_id].append(c)
        for calls in out.values():
            calls.sort(key=lambda c: (c.dep, c.seq))
        return out

    def _to_calls(self, feed, r, today: set[str], yday: set[str],
                  dep_range: tuple[int, int] | None = None):
        sid = r["service_id"]
        if sid not in today and sid not in yday:
            return
        arr, dep = tsec(r["arrival_time"]), tsec(r["departure_time"])
        if arr is None and dep is None:
            return
        arr = arr if arr is not None else dep
        dep = dep if dep is not None else arr
        for shift, services in ((0, today), (-DAY, yday)):
            if dep_range and not dep_range[0] <= dep + shift <= dep_range[1]:
                continue
            if sid in services:
                yield Call(feed, r["trip_id"], r["stop_id"], int(r["stop_sequence"]),
                           arr + shift, dep + shift, r["route_id"], r["headsign"],
                           r["shape_id"])

    # -- search ----------------------------------------------------------------------------
    def plan(self, origin: Point, destination: Point) -> dict:
        o_stops = self.stops_near(origin, "origin")
        d_stops = self.stops_near(destination, "destination")
        self.ends = {"origin": o_stops, "destination": d_stops}   # for the search log
        self._origin, self._destination = origin, destination
        base = {"origin": _pt(origin), "destination": _pt(destination),
                "now": self.midnight_epoch + self.now_s,
                "window_min": self.cfg.window_s // 60,
                "short_transfer_min": self.cfg.short_transfer_s // 60}
        if not o_stops or not d_stops:
            where = "start" if not o_stops else "destination"
            return {**base, "options": [], "outcome": "no_stops",
                    "notice": f"No bus or train stop within about a mile of your {where}."}
        if haversine_m(origin.lat, origin.lon, destination.lat, destination.lon) < 400:
            return {**base, "options": [], "outcome": "short_walk",
                    "notice": "Your destination is a short walk away."}

        direct = self._direct(o_stops, d_stops)
        # "Is there a good direct trip?" counts only ones the rider can actually catch,
        # and compares like the ranking does (a long walk counts against a direct trip).
        best_direct = min((self._score(c) for c in direct if self._feasible(c) == "ok"),
                          default=None)
        transfers = self._one_transfer(o_stops, d_stops)
        if best_direct is not None:
            transfers = [c for c in transfers if self._score(c)
                         <= best_direct - self.cfg.transfer_only_if_faster_s]
        good = self._choose(direct + transfers)
        if not good and self.cfg.max_transfers >= 2:
            good = self._choose(self._two_transfer())
        result = {**base, "options": [self._describe(c) for c in good],
                  "outcome": "ok" if good else "no_trips",
                  "live_checks": self.live_calls,
                  "live_available": self.realtime is not None and self.realtime.available()}
        if not good:
            result["notice"] = (f"No trips in the next {_span(self.cfg.window_s)} "
                                "from stops near you.")
        return result

    def _choose(self, cands: list[Candidate]) -> list[Candidate]:
        """The options to show: one departure per route choice, best first."""
        groups = self._groups(cands)
        # Live times for the next few departures of the most promising route choices.
        self._apply_realtime([c for g in groups[: self.cfg.max_options * 2] for c in g[:3]])
        good = []
        for g in groups:
            # The first departure with short transfers, else the first one that works.
            chosen = (next((c for c in g if self._feasible(c) == "ok"), None)
                      or next((c for c in g if self._feasible(c) == "long_transfer_wait"), None))
            if chosen:
                good.append(chosen)
        # Long waits at a transfer only when no option has short ones.
        short = [c for c in good if self._feasible(c) == "ok"]
        return sorted(self._drop_dominated(short or good),
                      key=self._score)[: self.cfg.max_options]

    def _window(self, walk_s: int) -> tuple[int, int]:
        # Starts a little before the rider can reach the stop: a late bus may become
        # catchable once live times are applied (then re-checked).
        start = self.now_s + walk_s
        return start - 300, start + self.cfg.window_s

    def _board(self, feed: str, o_walk: dict[str, int]) -> list[Call]:
        """Departures from the start stops the rider can reach in time (cached)."""
        if feed not in self._boardings:
            lo = self.now_s - 300
            hi = self.now_s + max(o_walk.values()) + self.cfg.window_s
            reachable = sorted(
                (c for c in self.calls_at(feed, list(o_walk), (lo, hi))
                 if self._window(o_walk[c.stop_id])[0] <= c.dep
                 <= self._window(o_walk[c.stop_id])[1]), key=lambda c: c.dep)
            # Only the next few departures of each route and direction at each stop: a
            # later bus on the same line never gets the rider there sooner.
            seen: dict[tuple, int] = defaultdict(int)
            self._boardings[feed] = []
            for c in reachable:
                key = (c.route_id, c.headsign, c.stop_id)
                if seen[key] < self.cfg.departures_per_route:
                    seen[key] += 1
                    self._boardings[feed].append(c)
        return self._boardings[feed]

    def _direct(self, o_stops, d_stops) -> list[Candidate]:
        out = []
        o_by = _by_feed(o_stops)
        d_by = _by_feed(d_stops)
        for feed in set(o_by) & set(d_by):
            o_walk = {s.stop_id: s.walk_s for s in o_by[feed]}
            d_walk = {s.stop_id: s.walk_s for s in d_by[feed]}
            board = self._board(feed, o_walk)
            if not board:
                continue
            trips = self.calls_of_trips(feed, [b.trip_id for b in board])
            for b in board:
                for a in trips[b.trip_id]:
                    if (a.stop_id in d_walk and a.seq > b.seq
                            and 0 < a.arr - b.dep <= self.cfg.max_ride_s):
                        out.append(Candidate(
                            [_ride(b, a)], o_walk[b.stop_id], d_walk[a.stop_id]))
        return out

    def _one_transfer(self, o_stops, d_stops) -> list[Candidate]:
        o_by, d_by = _by_feed(o_stops), _by_feed(d_stops)
        # Downstream of every trip leaving a start stop soon.
        down: list[tuple[Call, Call, int]] = []      # (board call, later call, walk_start)
        for feed, stops in o_by.items():
            o_walk = {s.stop_id: s.walk_s for s in stops}
            board = self._board(feed, o_walk)
            first = {}
            for b in board:  # one boarding per trip: the stop that lets the rider leave latest
                cur = first.get(b.trip_id)
                if cur is None or b.dep - o_walk[b.stop_id] > cur.dep - o_walk[cur.stop_id]:
                    first[b.trip_id] = b
            trips = self.calls_of_trips(feed, list(first))
            for tid, b in first.items():
                for c in trips.get(tid, ()):
                    if c.seq > b.seq and _same_run(c, b):
                        down.append((b, c, o_walk[b.stop_id]))
        self._down = down
        if not down:
            return []
        latest = max(c.arr for _, c, _ in down) + self.cfg.max_wait_s
        # Upstream of every trip reaching an end stop, indexed by stop.
        up: dict[tuple[str, str], list[tuple[Call, Call, int]]] = defaultdict(list)
        for feed, stops in d_by.items():
            d_walk = {s.stop_id: s.walk_s for s in stops}
            horizon = latest + self.cfg.max_ride_s
            arrive = [c for c in self.calls_at(feed, list(d_walk), (self.now_s, horizon + 3600))
                      if self.now_s < c.arr <= horizon]
            last = {}
            for a in arrive:  # one end stop per trip: the one that gets the rider there first
                cur = last.get(a.trip_id)
                if cur is None or a.arr + d_walk[a.stop_id] < cur.arr + d_walk[cur.stop_id]:
                    last[a.trip_id] = a
            trips = self.calls_of_trips(feed, list(last))
            for tid, a in last.items():
                for c in trips.get(tid, ()):
                    if c.seq < a.seq and _same_run(c, a) and self.now_s <= c.dep <= latest:
                        up[(feed, c.stop_id)].append((c, a, d_walk[a.stop_id]))
        self._up = up
        if not up:
            return []
        near = self._transfer_index(list(up), {(b.feed, x.stop_id) for b, x, _ in down})
        onward: dict[tuple[str, str], list] = {}
        out = []
        for b, x, walk_start in down:
            key = (b.feed, x.stop_id)
            if key not in onward:   # every onward departure within 0.3 km, in time order
                onward[key] = sorted(((walk, *u) for stop, walk in near.get(key, ())
                                      for u in up[stop]), key=lambda e: e[1].dep)
            taken = set()   # per onward route, only the first vehicle that can be caught
            for walk, y, a, walk_end in onward[key]:
                if y.dep > x.arr + self.cfg.max_wait_s + 600:
                    break
                route = (y.feed, y.route_id)
                if route == (b.feed, b.route_id) or route in taken:
                    continue
                slack = y.dep - (x.arr + walk)
                if self.cfg.min_transfer_s <= slack <= self.cfg.max_wait_s + 300:
                    taken.add(route)
                    out.append(Candidate([_ride(b, x), _ride(y, a)], walk_start, walk_end,
                                         [walk]))
        return out

    def _two_transfer(self) -> list[Candidate]:
        """Ride, change, ride, change, ride: the first ride from a start stop (as in
        ``_one_transfer``), a middle ride from a stop near where it ends to a stop near
        where a last ride to the end stops begins. Only for searches where nothing
        simpler works."""
        down, up = self._down, self._up
        if not down or not up:
            return []
        cfg, o, d = self.cfg, self._origin, self._destination
        max_wait = min(cfg.max_wait_s, cfg.two_transfer_wait_s)
        # Each change makes progress: the first one is closer to the end than the start
        # is, the last one closer to the start than the end is.
        span = haversine_m(o.lat, o.lon, d.lat, d.lon)
        self._load_stops([(x.feed, x.stop_id) for _, x, _ in down] + list(up))

        def closer(key, to: Point) -> bool:
            s = self.stop(*key)
            return s["lat"] is not None and haversine_m(s["lat"], s["lon"],
                                                        to.lat, to.lon) < span

        # Every first ride the rider can catch, by the stop it reaches.
        first: dict[tuple[str, str], list[tuple[Call, Call, int]]] = defaultdict(list)
        for b, x, walk_start in down:
            if x.arr - self.now_s > cfg.max_ride_s or b.dep < self.now_s + walk_start:
                continue
            first[(x.feed, x.stop_id)].append((b, x, walk_start))
        first = {k: v for k, v in first.items() if closer(k, d)}
        last = {k: v for k, v in up.items() if closer(k, o)}
        if not first or not last:
            return []
        around = self._stops_around(list(first) + list(last))
        # Where the last ride can be reached from: stops within a short walk of its
        # boarding stops. Only agencies with such a stop can give the middle ride.
        ends: dict[tuple[str, str], list[tuple[tuple[str, str], int]]] = defaultdict(list)
        for key in last:
            for other, walk in around.get(key, ()):
                ends[other].append((key, walk))
        for entries in last.values():
            entries.sort(key=lambda e: e[0].dep)
        end_feeds = {feed for feed, _ in ends}
        # Where the middle ride can start: (time the rider is there, first ride, walk),
        # in time order.
        ready: dict[tuple[str, str], list[tuple[int, tuple[Call, Call, int], int]]] = (
            defaultdict(list))
        for key, rides in first.items():
            for other, walk in around.get(key, ()):
                if other[0] in end_feeds:
                    ready[other].extend((x.arr + walk, (b, x, ws), walk) for b, x, ws in rides)
        for entries in ready.values():
            entries.sort(key=lambda e: e[0])

        out = []
        by_feed: dict[str, list[str]] = defaultdict(list)
        for feed, stop_id in ready:
            by_feed[feed].append(stop_id)
        for feed, stop_ids in by_feed.items():
            lo = min(ready[(feed, s)][0][0] for s in stop_ids) + cfg.min_transfer_s
            hi = max(ready[(feed, s)][-1][0] for s in stop_ids) + max_wait
            boards: dict[str, tuple[Call, tuple[int, tuple[Call, Call, int], int]]] = {}
            per_route: dict[tuple, int] = defaultdict(int)
            for c in sorted(self.calls_at(feed, stop_ids, (lo, hi)), key=lambda c: c.dep):
                if c.trip_id in boards:
                    continue
                # The latest first ride that still makes this departure: least waiting.
                fit = None
                for entry in ready[(feed, c.stop_id)]:
                    if entry[0] + cfg.min_transfer_s > c.dep:
                        break
                    fit = entry
                if fit is None or c.dep - fit[0] > max_wait:
                    continue
                b = fit[1][0]
                if (feed, c.route_id) == (b.feed, b.route_id):
                    continue
                group = (c.route_id, c.headsign, c.stop_id)
                if per_route[group] < 6:   # the next few departures of each line there
                    per_route[group] += 1
                    boards[c.trip_id] = (c, fit)
            trips = self.calls_of_trips(feed, list(boards))
            for tid, (m, (_, (b, x, walk_start), walk1)) in boards.items():
                taken = set()   # per last route, the first vehicle that can be caught
                for y in trips.get(tid, ()):
                    if y.seq <= m.seq or not _same_run(y, m) or (feed, y.stop_id) not in ends:
                        continue
                    for key, walk2 in ends[(feed, y.stop_id)]:
                        for c, a, walk_end in last[key]:
                            route = (c.feed, c.route_id)
                            if route in taken or route == (feed, m.route_id):
                                continue
                            slack = c.dep - (y.arr + walk2)
                            if slack < cfg.min_transfer_s:
                                continue
                            if slack > max_wait:
                                break
                            taken.add(route)
                            out.append(Candidate(
                                [_ride(b, x), _ride(m, y), _ride(c, a)], walk_start,
                                walk_end, [walk1, walk2]))
        return out

    def _stops_around(self, keys: list[tuple[str, str]]
                      ) -> dict[tuple[str, str], list[tuple[tuple[str, str], int]]]:
        """For each stop: every boardable stop (any agency) less than ``transfer_m``
        away, with the walk time — itself included, at no walk."""
        self._load_stops(keys)
        pts = [self.stop(*k) for k in keys]
        pts = [s for s in pts if s["lat"] is not None]
        if not pts:
            return {}
        pad = self.cfg.transfer_m / 111_320.0
        lat0, lat1 = min(s["lat"] for s in pts) - pad, max(s["lat"] for s in pts) + pad
        lon0, lon1 = (min(s["lon"] for s in pts) - 2 * pad,
                      max(s["lon"] for s in pts) + 2 * pad)
        cell_lat = pad
        grid: dict[tuple[int, int], list[tuple[tuple[str, str], float, float]]] = defaultdict(list)

        def cell(lat, lon) -> tuple[int, int]:
            lon_deg = cell_lat / max(math.cos(math.radians(lat)), 0.1)
            return int(lat // cell_lat), int(lon // lon_deg)

        for r in self.db.conn.execute(
            """
            SELECT feed_id, stop_id, lat, lon FROM gtfs_stops
             WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
               AND (location_type IS NULL OR location_type IN (0, ''))
            """, (lat0, lat1, lon0, lon1)):
            grid[cell(r["lat"], r["lon"])].append(((r["feed_id"], r["stop_id"]),
                                                   r["lat"], r["lon"]))
        out = {}
        for key in keys:
            s = self.stop(*key)
            if s["lat"] is None:
                continue
            ci, cj = cell(s["lat"], s["lon"])
            found = []
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for other, lat, lon in grid.get((ci + di, cj + dj), ()):
                        dist = haversine_m(s["lat"], s["lon"], lat, lon)
                        if dist <= self.cfg.transfer_m:
                            found.append((other, 0 if other == key
                                          else max(60, self.walk_s(dist))))
            out[key] = found
        return out

    def _transfer_index(self, onward_stops: list[tuple[str, str]],
                        arrival_stops: set[tuple[str, str]]
                        ) -> dict[tuple[str, str], list[tuple[tuple[str, str], int]]]:
        """For each stop a rider may get off at: the onward stops less than
        ``transfer_m`` away (any agency), with the walk time. Grid lookup, ~0.3 km cells."""
        self._load_stops(onward_stops + list(arrival_stops))
        cell_lat = self.cfg.transfer_m / 111_320.0
        grid: dict[tuple[int, int], list[tuple[str, str]]] = defaultdict(list)

        def cell(s: dict) -> tuple[int, int]:
            lon_deg = cell_lat / max(math.cos(math.radians(s["lat"])), 0.1)
            return int(s["lat"] // cell_lat), int(s["lon"] // lon_deg)

        for key in onward_stops:
            s = self.stop(*key)
            if s["lat"] is not None:
                grid[cell(s)].append(key)
        out = {}
        for key in arrival_stops:
            s = self.stop(*key)
            if s["lat"] is None:
                continue
            ci, cj = cell(s)
            found = []
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for other in grid.get((ci + di, cj + dj), ()):
                        o = self.stop(*other)
                        d = haversine_m(s["lat"], s["lon"], o["lat"], o["lon"])
                        if d <= self.cfg.transfer_m:
                            found.append((other, 0 if other == key else max(60, self.walk_s(d))))
            out[key] = found
        return out

    def _load_stops(self, keys: list[tuple[str, str]]) -> None:
        """Fill the stop cache for many stops at once."""
        by_feed: dict[str, list[str]] = defaultdict(list)
        for feed, stop_id in keys:
            if (feed, stop_id) not in self._stop_cache:
                by_feed[feed].append(stop_id)
        for feed, ids in by_feed.items():
            ids = list(dict.fromkeys(ids))
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in self.db.conn.execute(
                    f"""
                    SELECT s.stop_id, s.name, s.lat, s.lon, s.code, p.name AS parent_name
                      FROM gtfs_stops s
                      LEFT JOIN gtfs_stops p ON p.feed_id = s.feed_id
                                            AND p.stop_id = s.parent_station
                     WHERE s.feed_id = ? AND s.stop_id IN ({','.join('?' * len(chunk))})
                    """, (feed, *chunk)):
                    self._stop_cache[(feed, r["stop_id"])] = dict(r)
            for stop_id in ids:
                self._stop_cache.setdefault((feed, stop_id), {
                    "stop_id": stop_id, "name": stop_id, "lat": None, "lon": None})

    def _link(self, c: Candidate, end: str) -> NearStop | None:
        ride = c.rides[0] if end == "origin" else c.rides[-1]
        stop = ride.board_stop if end == "origin" else ride.alight_stop
        return self._links[end].get((ride.feed, stop))

    def _shuttle_s(self, c: Candidate) -> int:
        return sum(s.link_s for s in (self._link(c, "origin"), self._link(c, "destination"))
                   if s)

    def _score(self, c: Candidate) -> int:
        # Earliest arrival, with each transfer counted as 5 extra minutes and walking
        # counted twice (riders would rather wait than walk far; an airport shuttle
        # ride isn't walking).
        return (c.arrive + 300 * (len(c.rides) - 1) + c.walk_start_s + c.walk_end_s
                - self._shuttle_s(c))

    def _groups(self, cands: list[Candidate], per_group: int = 4) -> list[list[Candidate]]:
        """Candidates grouped by route choice (e.g. Blue Line → Silver Line), each group in
        departure order (the first catchable one wins later), groups best first. Keeping
        several departures per group means a just-missed train doesn't hide the next."""
        by_sig: dict[tuple, list[Candidate]] = defaultdict(list)
        for c in cands:
            by_sig[c.signature()].append(c)
        groups = []
        for members in by_sig.values():
            # earliest arrival for each departure time, then in departure order
            members.sort(key=lambda c: (c.rides[0].dep, c.arrive))
            seen, kept = set(), []
            for c in members:
                if c.rides[0].dep not in seen:
                    seen.add(c.rides[0].dep)
                    kept.append(c)
            groups.append(kept[:per_group])
        catchable = {id(g): min((self._score(c) for c in g
                                 if self._feasible(c) in ("ok", "long_transfer_wait")),
                                default=None) for g in groups}
        return sorted(groups, key=lambda g: (catchable[id(g)] is None,
                                             catchable[id(g)] or min(map(self._score, g))))

    @staticmethod
    def _drop_dominated(options: list[Candidate]) -> list[Candidate]:
        """Two options ending on the same vehicle: keep the one that leaves later (less
        time spent), then the one with fewer transfers."""
        best: dict[tuple, Candidate] = {}
        for c in options:
            key = (c.rides[-1].feed, c.rides[-1].trip_id)
            leave = c.rides[0].dep_eff - c.walk_start_s
            cur = best.get(key)
            if cur is None or (leave, -len(c.rides)) > (cur.rides[0].dep_eff - cur.walk_start_s,
                                                        -len(cur.rides)):
                best[key] = c
        return list(best.values())

    # -- live times + filter -------------------------------------------------------------------
    def _apply_realtime(self, cands: list[Candidate]) -> None:
        if self.realtime is None:
            return
        for c in cands:
            for ride in c.rides:
                if not self.agencies.get(ride.feed, {}).get("has_realtime"):
                    continue
                # Per-search cap applies only to checks that cost a Transitland call.
                if self.live_calls >= self.cfg.max_live_calls and not self.realtime.free(
                        ride.feed, ride.board_stop):
                    continue
                hit, calls = self.realtime.lookup(ride.feed, ride.board_stop, ride.trip_id,
                                                  ride.board_seq, ride.dep, self.today)
                self.live_calls += calls
                if hit:
                    ride.live = hit.get("live", False)
                    ride.cancelled = hit.get("cancelled", False)
                    if hit.get("dep_s") is not None:
                        ride.delay = hit["dep_s"] - ride.dep
                    elif hit.get("delay") is not None:
                        ride.delay = int(hit["delay"])

    def _feasible(self, c: Candidate) -> str:
        if any(r.cancelled for r in c.rides):
            return "cancelled"
        if c.rides[0].dep_eff < self.now_s + c.walk_start_s:
            return "missed"
        long_wait = False
        for prev, nxt, walk in zip(c.rides, c.rides[1:], c.transfer_walk_s):
            slack = nxt.dep_eff - (prev.arr_eff + walk)
            if slack < self.cfg.min_transfer_s:
                return "missed_connection"
            if slack > self.cfg.max_wait_s:
                return "missed_connection"     # outside the search: treat as not working
            if slack > self.cfg.short_transfer_s:
                long_wait = True
        return "long_transfer_wait" if long_wait else "ok"

    # -- output ------------------------------------------------------------------------------
    def _epoch(self, s: int) -> int:
        return self.midnight_epoch + s

    def _describe(self, c: Candidate) -> dict:
        legs: list[dict] = []
        first = c.rides[0]
        start_stop = self.stop(first.feed, first.board_stop)
        legs.append({"mode": "walk", "duration_s": c.walk_start_s,
                     "to": _stop_pt(start_stop), **_airport(self._link(c, "origin"), "origin")})
        for i, ride in enumerate(c.rides):
            if i:
                walk = c.transfer_walk_s[i - 1]
                legs.append({
                    "mode": "walk", "duration_s": walk, "transfer": True,
                    "wait_s": ride.dep_eff - (c.rides[i - 1].arr_eff + walk),
                    "to": _stop_pt(self.stop(ride.feed, ride.board_stop)),
                })
            legs.append(self._ride_leg(ride))
        last = c.rides[-1]
        legs.append({"mode": "walk", "duration_s": c.walk_end_s,
                     "from": _stop_pt(self.stop(last.feed, last.alight_stop)),
                     **_airport(self._link(c, "destination"), "destination")})
        leave = first.dep_eff - c.walk_start_s
        fares = [leg.get("fare") for leg in legs if leg["mode"] == "ride"]
        return {
            "id": "|".join(f"{r.feed}:{r.trip_id}" for r in c.rides),
            "leave_at": self._epoch(leave),
            "depart_at": self._epoch(first.dep_eff),
            "arrive_at": self._epoch(c.arrive),
            "duration_s": c.arrive - (self.now_s if leave < self.now_s else leave),
            "wait_s": first.dep_eff - (self.now_s + c.walk_start_s),
            "walk_s": (c.walk_start_s + c.walk_end_s + sum(c.transfer_walk_s)
                       - self._shuttle_s(c)),
            "transfers": len(c.rides) - 1,
            "live": any(r.live for r in c.rides),
            "fare_estimate": (round(sum(fares), 2) if fares and all(
                isinstance(f, (int, float)) for f in fares) else None),
            "legs": legs,
        }

    def _ride_leg(self, ride: Ride) -> dict:
        route = self.db.conn.execute(
            "SELECT short_name, long_name, color, text_color, route_type FROM gtfs_routes "
            "WHERE feed_id = ? AND route_id = ?", (ride.feed, ride.route_id)).fetchone()
        calls = [c for c in self.calls_of_trips(ride.feed, [ride.trip_id]).get(ride.trip_id, ())
                 if ride.board_seq <= c.seq <= ride.alight_seq
                 and abs(c.dep - ride.dep) < DAY // 2]
        stops = [self.stop(ride.feed, c.stop_id) for c in calls]
        path = [[s["lon"], s["lat"]] for s in stops if s["lat"] is not None]
        shaped = self._shape_slice(ride, stops)
        agency = self.agencies.get(ride.feed, {})
        rtype = int(route["route_type"]) if route and route["route_type"] is not None else 3
        return {
            "mode": "ride",
            "agency": ride.feed,
            "agency_name": agency.get("name", ride.feed),
            "route_id": ride.route_id,
            "route_short_name": route["short_name"] if route else None,
            "route_long_name": route["long_name"] if route else None,
            "route_type": ROUTE_MODES.get(rtype, "bus"),
            "color": _hex(route["color"] if route else None, "#2b6f77"),
            "text_color": _hex(route["text_color"] if route else None, "#ffffff"),
            "headsign": ride.headsign,
            "trip_id": ride.trip_id,
            "board": {**_stop_pt(stops[0] if stops else self.stop(ride.feed, ride.board_stop)),
                      "scheduled": self._epoch(ride.dep), "expected": self._epoch(ride.dep_eff)},
            "alight": {**_stop_pt(stops[-1] if stops else self.stop(ride.feed,
                                                                    ride.alight_stop)),
                       "scheduled": self._epoch(ride.arr), "expected": self._epoch(ride.arr_eff)},
            "stops": max(0, len(calls) - 1),
            "live": ride.live,
            "delay_s": ride.delay if ride.live else None,
            "fare": agency.get("base_fare"),
            "geometry": shaped or path,
        }

    def _shape_slice(self, ride: Ride, stops: list[dict]) -> list[list[float]] | None:
        if not ride.shape_id or not stops or stops[0]["lat"] is None:
            return None
        row = self.db.conn.execute(
            "SELECT polyline FROM gtfs_shapes WHERE feed_id = ? AND shape_id = ?",
            (ride.feed, ride.shape_id)).fetchone()
        if not row:
            return None
        pts = decode(row["polyline"])
        if len(pts) < 2:
            return None

        def nearest(lat, lon, start=0):
            best, idx = float("inf"), start
            for i in range(start, len(pts)):
                d = (pts[i][0] - lat) ** 2 + (pts[i][1] - lon) ** 2
                if d < best:
                    best, idx = d, i
            return idx
        a = nearest(stops[0]["lat"], stops[0]["lon"])
        b = nearest(stops[-1]["lat"], stops[-1]["lon"], a)
        if b <= a:
            return None
        return [[lon, lat] for lat, lon in pts[a:b + 1]]


# -- helpers ---------------------------------------------------------------------------------
def _span(seconds: int) -> str:
    minutes = seconds // 60
    if minutes >= 120 and minutes % 60 == 0:
        return f"{minutes // 60} hours"
    return f"{minutes} minutes"


def _by_feed(stops: list[NearStop]) -> dict[str, list[NearStop]]:
    out: dict[str, list[NearStop]] = defaultdict(list)
    for s in stops:
        out[s.feed].append(s)
    return out


def _same_run(a: Call, b: Call) -> bool:
    """Two calls of one trip on the same day: a trip whose service runs yesterday and
    today has both runs' calls (today's times and yesterday's, 24 h earlier)."""
    return abs(a.dep - b.dep) < DAY // 2


def _ride(board: Call, alight: Call) -> Ride:
    return Ride(board.feed, board.trip_id, board.route_id, board.headsign, board.shape_id,
                board.stop_id, board.seq, board.dep, alight.stop_id, alight.seq, alight.arr)


def _airport(stop: NearStop | None, end: str) -> dict:
    """The airport step on a start/end leg: directions, and how much is shuttle."""
    if stop is None or stop.link is None:
        return {}
    return {"airport": {**stop.link.info(end), "shuttle_s": stop.link_s}}


def _pt(p: Point) -> dict:
    return {"lat": p.lat, "lon": p.lon, "label": p.label}


def _stop_pt(s: dict) -> dict:
    return {"stop_id": s.get("stop_id"), "name": s.get("parent_name") or s.get("name"),
            "code": s.get("code"), "lat": s.get("lat"), "lon": s.get("lon")}


def _hex(value: str | None, default: str) -> str:
    v = (value or "").strip().lstrip("#")
    return f"#{v}" if len(v) == 6 else default
