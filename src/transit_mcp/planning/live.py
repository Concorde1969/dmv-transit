"""Leave-now trip planner for the map: real stops, today's timetable, live departures.

Flow (the site owner's design):
1. Find boardable stops within ``TRIP_STOP_RADIUS_M`` (0.3 km) of the start and the end.
2. From today's GTFS timetable (local database), find **direct** trips — a trip that
   leaves a start stop soon and later calls at an end stop.
3. Only when needed (no direct trip, or a much faster option exists), find **one-
   transfer** trips: ride to a stop, walk within the same stop cluster (every pair of
   stops in a cluster is < 0.3 km apart, see ``ingest/gtfs/clusters.py``), ride on.
4. Check **live departures** (Transitland, merged GTFS-realtime) for the boarding stops
   of the best options, on feeds that have realtime; delays carry forward along a trip.
5. Throw out any option that needs a wait of more than ``TRIP_MAX_WAIT_MIN`` (10 min)
   at the start stop or at a transfer, or a connection that can't be made.

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

DAY = 86_400
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ROUTE_MODES = {0: "tram", 1: "subway", 2: "rail", 3: "bus", 4: "ferry", 5: "cable_tram",
               6: "aerial", 7: "funicular", 11: "trolleybus", 12: "monorail"}


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
    walk_s: int


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
    radius_m: int = 300
    max_wait_s: int = 600
    min_transfer_s: int = 60
    window_s: int = 3600
    max_ride_s: int = 3 * 3600
    walk_m_per_min: float = 70.0
    detour: float = 1.25          # walking distance vs straight line
    max_options: int = 6
    transfer_only_if_faster_s: int = 600
    max_live_calls: int = 8

    @classmethod
    def from_settings(cls, s) -> LiveSettings:
        return cls(radius_m=s.trip_stop_radius_m, max_wait_s=s.trip_max_wait_min * 60,
                   window_s=s.trip_window_min * 60, walk_m_per_min=s.trip_walk_m_per_min,
                   max_live_calls=s.trip_max_live_calls)


class LivePlanner:
    def __init__(self, db: Database, *, cfg: LiveSettings | None = None,
                 realtime=None, agencies: dict | None = None,
                 tz: str = "America/New_York", now: datetime | None = None):
        self.db = db
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
        self._cluster: dict[tuple[str, str], int] | None = None
        self._stop_cache: dict[tuple[str, str], dict] = {}
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

    def cluster_of(self, feed: str, stop_id: str) -> int | None:
        if self._cluster is None:
            self._cluster = {(r[0], r[1]): r[2] for r in self.db.conn.execute(
                "SELECT feed_id, stop_id, cluster_id FROM cluster_stops")}
        return self._cluster.get((feed, stop_id))

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

    def stops_near(self, p: Point) -> list[NearStop]:
        r = self.cfg.radius_m
        dlat = r / 111_320.0
        dlon = r / (111_320.0 * max(math.cos(math.radians(p.lat)), 0.1))
        out = []
        for row in self.db.conn.execute(
            """
            SELECT s.feed_id, s.stop_id, s.name, s.lat, s.lon, par.name AS parent_name
              FROM gtfs_stops s
              LEFT JOIN gtfs_stops par ON par.feed_id = s.feed_id
                                      AND par.stop_id = s.parent_station
             WHERE s.lat BETWEEN ? AND ? AND s.lon BETWEEN ? AND ?
               AND (s.location_type IS NULL OR s.location_type IN (0, ''))
            """,
            (p.lat - dlat, p.lat + dlat, p.lon - dlon, p.lon + dlon),
        ):
            d = haversine_m(p.lat, p.lon, row["lat"], row["lon"])
            if d <= r:
                out.append(NearStop(row["feed_id"], row["stop_id"],
                                    row["parent_name"] or row["name"], row["lat"], row["lon"],
                                    self.walk_s(d)))
        return out

    # -- timetable queries ---------------------------------------------------------------
    def calls_at(self, feed: str, stop_ids: list[str]) -> list[Call]:
        """Every call at these stops today (incl. yesterday's after-midnight trips)."""
        if not stop_ids:
            return []
        today = self.services(feed, self.today)
        yday = self.services(feed, self.today - timedelta(days=1))
        out: list[Call] = []
        for i in range(0, len(stop_ids), 500):
            chunk = stop_ids[i:i + 500]
            for r in self.db.conn.execute(
                f"""
                SELECT st.trip_id, st.stop_id, st.stop_sequence, st.arrival_time,
                       st.departure_time, t.service_id, t.route_id, t.headsign, t.shape_id
                  FROM gtfs_stop_times st
                  JOIN gtfs_trips t ON t.feed_id = st.feed_id AND t.trip_id = st.trip_id
                 WHERE st.feed_id = ? AND st.stop_id IN ({','.join('?' * len(chunk))})
                """, (feed, *chunk),
            ):
                out.extend(self._to_calls(feed, r, today, yday))
        return out

    def calls_of_trips(self, feed: str, trip_ids: list[str]) -> dict[str, list[Call]]:
        """All calls of these trips, in stop order (service day already decided)."""
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

    def _to_calls(self, feed, r, today: set[str], yday: set[str]):
        arr, dep = tsec(r["arrival_time"]), tsec(r["departure_time"])
        if arr is None and dep is None:
            return
        arr = arr if arr is not None else dep
        dep = dep if dep is not None else arr
        for shift, services in ((0, today), (-DAY, yday)):
            if r["service_id"] in services:
                yield Call(feed, r["trip_id"], r["stop_id"], int(r["stop_sequence"]),
                           arr + shift, dep + shift, r["route_id"], r["headsign"],
                           r["shape_id"])

    # -- search ----------------------------------------------------------------------------
    def plan(self, origin: Point, destination: Point) -> dict:
        o_stops, d_stops = self.stops_near(origin), self.stops_near(destination)
        base = {"origin": _pt(origin), "destination": _pt(destination),
                "now": self.midnight_epoch + self.now_s,
                "radius_m": self.cfg.radius_m, "max_wait_min": self.cfg.max_wait_s // 60}
        if not o_stops or not d_stops:
            where = "start" if not o_stops else "destination"
            return {**base, "options": [], "later_options": [],
                    "notice": f"No transit stop within {self.cfg.radius_m} m of your {where}."}
        if haversine_m(origin.lat, origin.lon, destination.lat, destination.lon) < 400:
            return {**base, "options": [], "later_options": [],
                    "notice": "Your destination is a short walk away."}

        direct = self._direct(o_stops, d_stops)
        # "Is there a good direct trip?" counts only ones the rider can actually catch.
        best_direct = min((c.arrive for c in direct if self._feasible(c) == "ok"),
                          default=None)
        transfers = self._one_transfer(o_stops, d_stops)
        if best_direct is not None:
            transfers = [c for c in transfers
                         if c.arrive <= best_direct - self.cfg.transfer_only_if_faster_s]
        groups = self._groups(direct + transfers)

        # Live times for the next few departures of the most promising route choices.
        self._apply_realtime([c for g in groups[: self.cfg.max_options * 2] for c in g[:3]])
        good, later = [], []
        for g in groups:
            chosen = next((c for c in g if self._feasible(c) == "ok"), None)
            if chosen:
                good.append(chosen)
            elif len(later) < 2:
                waiting = next((c for c in g if self._feasible(c) == "long_wait"), None)
                if waiting:
                    later.append(waiting)
        good = sorted(self._drop_dominated(good), key=self._score)[: self.cfg.max_options]
        result = {**base, "options": [self._describe(c) for c in good],
                  "later_options": [] if good else [self._describe(c) for c in later],
                  "live_checks": self.live_calls,
                  "live_available": self.realtime is not None and self.realtime.available()}
        if not good:
            result["notice"] = (f"No trip leaves within {self.cfg.max_wait_s // 60} minutes "
                                "of you reaching a stop.")
        return result

    def _window(self, walk_s: int) -> tuple[int, int]:
        # Scheduled times a little outside the 0–10 min wait still qualify here: a late
        # bus may become catchable once live times are applied (then re-checked).
        start = self.now_s + walk_s
        return start - 300, start + self.cfg.max_wait_s + 300

    def _direct(self, o_stops, d_stops) -> list[Candidate]:
        out = []
        o_by = _by_feed(o_stops)
        d_by = _by_feed(d_stops)
        for feed in set(o_by) & set(d_by):
            o_walk = {s.stop_id: s.walk_s for s in o_by[feed]}
            d_walk = {s.stop_id: s.walk_s for s in d_by[feed]}
            board = [c for c in self.calls_at(feed, list(o_walk))
                     if self._window(o_walk[c.stop_id])[0] <= c.dep
                     <= self._window(o_walk[c.stop_id])[1]]
            if not board:
                continue
            alight: dict[str, list[Call]] = defaultdict(list)
            for c in self.calls_at(feed, list(d_walk)):
                alight[c.trip_id].append(c)
            for b in board:
                for a in alight.get(b.trip_id, ()):
                    if a.seq > b.seq and 0 < a.arr - b.dep <= self.cfg.max_ride_s:
                        out.append(Candidate(
                            [_ride(b, a)], o_walk[b.stop_id], d_walk[a.stop_id]))
        return out

    def _one_transfer(self, o_stops, d_stops) -> list[Candidate]:
        o_by, d_by = _by_feed(o_stops), _by_feed(d_stops)
        # Downstream of every trip leaving a start stop soon.
        down: list[tuple[Call, Call, int]] = []      # (board call, later call, walk_start)
        for feed, stops in o_by.items():
            o_walk = {s.stop_id: s.walk_s for s in stops}
            board = [c for c in self.calls_at(feed, list(o_walk))
                     if self._window(o_walk[c.stop_id])[0] <= c.dep
                     <= self._window(o_walk[c.stop_id])[1]]
            first = {}
            for b in board:  # one boarding per trip: the earliest-reachable start stop
                if b.trip_id not in first or b.dep < first[b.trip_id].dep:
                    first[b.trip_id] = b
            trips = self.calls_of_trips(feed, list(first))
            for tid, b in first.items():
                for c in trips.get(tid, ()):
                    if c.seq > b.seq:
                        down.append((b, c, o_walk[b.stop_id]))
        if not down:
            return []
        latest = max(c.arr for _, c, _ in down) + self.cfg.max_wait_s
        # Upstream of every trip reaching an end stop, indexed by cluster.
        up: dict[int, list[tuple[Call, Call, int]]] = defaultdict(list)
        for feed, stops in d_by.items():
            d_walk = {s.stop_id: s.walk_s for s in stops}
            arrive = [c for c in self.calls_at(feed, list(d_walk))
                      if self.now_s < c.arr <= latest + self.cfg.max_ride_s]
            last = {}
            for a in arrive:
                if a.trip_id not in last or a.arr < last[a.trip_id].arr:
                    last[a.trip_id] = a
            trips = self.calls_of_trips(feed, list(last))
            for tid, a in last.items():
                for c in trips.get(tid, ()):
                    if c.seq < a.seq and self.now_s <= c.dep <= latest:
                        cl = self.cluster_of(feed, c.stop_id)
                        if cl is not None:
                            up[cl].append((c, a, d_walk[a.stop_id]))
        out = []
        for b, x, walk_start in down:
            cl = self.cluster_of(b.feed, x.stop_id)
            if cl is None or cl not in up:
                continue
            for y, a, walk_end in up[cl]:
                if (b.feed, b.route_id) == (y.feed, y.route_id):
                    continue
                walk = self._transfer_walk(b.feed, x.stop_id, y.feed, y.stop_id)
                slack = y.dep - (x.arr + walk)
                if self.cfg.min_transfer_s <= slack <= self.cfg.max_wait_s + 300:
                    out.append(Candidate([_ride(b, x), _ride(y, a)], walk_start, walk_end,
                                         [walk]))
        return out

    def _transfer_walk(self, f1, s1, f2, s2) -> int:
        if (f1, s1) == (f2, s2):
            return 0
        a, b = self.stop(f1, s1), self.stop(f2, s2)
        if a["lat"] is None or b["lat"] is None:
            return 300
        return max(60, self.walk_s(haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])))

    @staticmethod
    def _score(c: Candidate) -> int:
        return c.arrive + 300 * (len(c.rides) - 1) + (c.walk_start_s + c.walk_end_s) // 2

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
        catchable = {id(g): min((self._score(c) for c in g if self._feasible(c) == "ok"),
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
                if self.live_calls >= self.cfg.max_live_calls and not self.realtime.cached(
                        ride.feed, ride.board_stop):
                    continue
                deps, fetched = self.realtime.stop_departures(ride.feed, ride.board_stop,
                                                              self.today)
                self.live_calls += int(fetched)
                hit = (deps or {}).get(ride.trip_id)
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
        wait = c.rides[0].dep_eff - (self.now_s + c.walk_start_s)
        if wait < 0:
            return "missed"
        if wait > self.cfg.max_wait_s:
            return "long_wait"
        for prev, nxt, walk in zip(c.rides, c.rides[1:], c.transfer_walk_s):
            slack = nxt.dep_eff - (prev.arr_eff + walk)
            if slack < self.cfg.min_transfer_s:
                return "missed_connection"
            if slack > self.cfg.max_wait_s:
                return "long_transfer_wait"
        return "ok"

    # -- output ------------------------------------------------------------------------------
    def _epoch(self, s: int) -> int:
        return self.midnight_epoch + s

    def _describe(self, c: Candidate) -> dict:
        legs: list[dict] = []
        first = c.rides[0]
        start_stop = self.stop(first.feed, first.board_stop)
        legs.append({"mode": "walk", "duration_s": c.walk_start_s,
                     "to": _stop_pt(start_stop)})
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
                     "from": _stop_pt(self.stop(last.feed, last.alight_stop))})
        leave = first.dep_eff - c.walk_start_s
        fares = [leg.get("fare") for leg in legs if leg["mode"] == "ride"]
        return {
            "id": "|".join(f"{r.feed}:{r.trip_id}" for r in c.rides),
            "leave_at": self._epoch(leave),
            "depart_at": self._epoch(first.dep_eff),
            "arrive_at": self._epoch(c.arrive),
            "duration_s": c.arrive - (self.now_s if leave < self.now_s else leave),
            "wait_s": first.dep_eff - (self.now_s + c.walk_start_s),
            "walk_s": c.walk_start_s + c.walk_end_s + sum(c.transfer_walk_s),
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
                 if ride.board_seq <= c.seq <= ride.alight_seq]
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
def _by_feed(stops: list[NearStop]) -> dict[str, list[NearStop]]:
    out: dict[str, list[NearStop]] = defaultdict(list)
    for s in stops:
        out[s.feed].append(s)
    return out


def _ride(board: Call, alight: Call) -> Ride:
    return Ride(board.feed, board.trip_id, board.route_id, board.headsign, board.shape_id,
                board.stop_id, board.seq, board.dep, alight.stop_id, alight.seq, alight.arr)


def _pt(p: Point) -> dict:
    return {"lat": p.lat, "lon": p.lon, "label": p.label}


def _stop_pt(s: dict) -> dict:
    return {"stop_id": s.get("stop_id"), "name": s.get("parent_name") or s.get("name"),
            "code": s.get("code"), "lat": s.get("lat"), "lon": s.get("lon")}


def _hex(value: str | None, default: str) -> str:
    v = (value or "").strip().lstrip("#")
    return f"#{v}" if len(v) == 6 else default
