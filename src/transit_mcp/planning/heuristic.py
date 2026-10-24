"""Built-in planner: a feasibility-and-cost connection advisor.

Not a time-routed journey planner. It finds which systems serve each end, picks good
transfer hubs (frequent systems such as Metro first), states which days the
connection actually works, and adds up the fare for each leg with how to pay it.
This is the default engine and the fallback when an external engine is unavailable.

Every leg is verified against the data before it is offered:

* Metrorail legs come from :mod:`transit_mcp.retrieval.metro` (real line order), so
  a change of train is never hidden.
* Bus/rail legs from GTFS agencies must have a scheduled trip that serves the
  boarding stop *before* the alighting stop; the days a leg runs come from those
  trips' service calendars.
"""

from __future__ import annotations

import json
from datetime import datetime

from transit_mcp.db.database import Database
from transit_mcp.planning.models import Leg, TripOption, TripPlan, TripRequest
from transit_mcp.retrieval.metro import route_between_codes
from transit_mcp.retrieval.queries import get_fare, is_stale
from transit_mcp.retrieval.regional import _DAY_FULL, _day_filter, _haversine_m

_TIER_RANK = {"frequent": 0, "peak": 1, "intercity": 2, "infrequent": 3, None: 4}
_ALL_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri"}
_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CAL_COLS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class HeuristicPlanner:
    name = "heuristic"

    def __init__(self, db_path):
        self._db_path = db_path

    def describe(self) -> dict:
        return {
            "name": self.name,
            "version": "2",
            "kind": "built-in",
            "capabilities": ["feasibility_by_day", "fares", "one_transfer",
                             "metrorail_line_changes", "verified_routes"],
            "notes": "Connection advisor from stop proximity, scheduled trips and "
            "service calendars; does not compute exact departure times.",
        }

    def plan(self, request: TripRequest) -> TripPlan:
        with Database(self._db_path) as db:
            return _Planner(db, request).plan()


# ---------------------------------------------------------------------------
def _days_label(days: set[str]) -> str:
    if days == _ALL_DAYS:
        return "every day"
    if days == _WEEKDAYS:
        return "Mon–Fri"
    if days == {"sat", "sun"}:
        return "weekends"
    return ", ".join(_DAY_FULL[d] for d in _ORDER if d in days)


def _feasibility(day_sets: list[set[str]], day: str | None) -> tuple[bool, str]:
    common = set(_ALL_DAYS)
    for s in day_sets:
        common &= s
    if not common:
        return False, "the legs have no day of the week in common"
    label = _days_label(common)
    if day:
        key = _day_filter(day)
        if key == "weekend" and not (common & {"sat", "sun"}):
            return False, f"this connection has no weekend service (runs {label})"
        if key == "weekday" and not (common & _WEEKDAYS):
            return False, f"this connection is weekend-only (runs {label})"
        if key and key.startswith("runs_") and key[5:] not in common:
            full = _DAY_FULL.get(key[5:], key[5:].title())
            return False, f"no {full} service on this connection (runs {label})"
    return True, f"works {label}"


class _Planner:
    def __init__(self, db: Database, req: TripRequest):
        self.db, self.req = db, req
        self.meta: dict[str, dict] = {}
        for table, key in (("agency_profiles", "profile"), ("fare_vending", "fare"),
                           ("gtfs_feeds", "feed")):
            for r in db.conn.execute(f"SELECT * FROM {table}"):
                self.meta.setdefault(r["feed_id"], {})[key] = dict(r)
        self._masks: dict[str, dict[str, set[str]]] = {}
        self._routes: dict[tuple[str, str], str] = {}

    # -- agency metadata -------------------------------------------------------
    def name(self, ag: str) -> str:
        m = self.meta.get(ag, {})
        return (m.get("profile", {}).get("display_name")
                or m.get("feed", {}).get("agency_name") or ag)

    def role(self, ag: str) -> str:
        return self.meta.get(ag, {}).get("profile", {}).get("role", "") or ""

    def tier(self, ag: str) -> int:
        return _TIER_RANK.get(self.meta.get(ag, {}).get("profile", {}).get("frequency_tier"), 4)

    def fare(self, ag: str) -> dict:
        return self.meta.get(ag, {}).get("fare", {})

    # -- service days from trips ---------------------------------------------------
    def service_days(self, feed: str, service_ids: set[str]) -> set[str]:
        if feed not in self._masks:
            masks: dict[str, set[str]] = {}
            for r in self.db.conn.execute(
                f"SELECT service_id, {', '.join(_CAL_COLS)} FROM gtfs_calendar "
                "WHERE feed_id = ?", (feed,),
            ):
                masks[r["service_id"]] = {d for d, c in zip(_ORDER, _CAL_COLS) if r[c]}
            for r in self.db.conn.execute(
                "SELECT service_id, date FROM gtfs_calendar_dates "
                "WHERE feed_id = ? AND exception_type = 1", (feed,),
            ):
                try:
                    dow = _ORDER[datetime.strptime(r["date"], "%Y%m%d").weekday()]
                except ValueError:
                    continue
                masks.setdefault(r["service_id"], set()).add(dow)
            self._masks[feed] = masks
        days: set[str] = set()
        for sid in service_ids:
            days |= self._masks[feed].get(sid, set())
        return days

    def route_label(self, feed: str, route_id: str) -> str:
        key = (feed, route_id)
        if key not in self._routes:
            r = self.db.conn.execute(
                "SELECT short_name, long_name FROM gtfs_routes WHERE feed_id=? AND route_id=?",
                key,
            ).fetchone()
            label = " ".join(x for x in ((r["short_name"], r["long_name"]) if r else ()) if x)
            self._routes[key] = label or route_id
        return self._routes[key]

    def stop_name(self, feed: str, stop_id: str) -> str:
        r = self.db.conn.execute(
            "SELECT name FROM gtfs_stops WHERE feed_id=? AND stop_id=?", (feed, stop_id)
        ).fetchone()
        return r["name"] if r else stop_id

    # -- verified GTFS connections -------------------------------------------------
    def _trips(self, feed: str, from_ids: list[str], to_ids: list[str],
               group_by: str) -> dict[str, dict]:
        """Scheduled trips that serve a ``from`` stop before a ``to`` stop, best per
        ``group_by`` stop ('to' = per reachable destination, 'from' = per origin)."""
        if not from_ids or not to_ids:
            return {}
        fm, tm = ",".join("?" * len(from_ids)), ",".join("?" * len(to_ids))
        rows = self.db.conn.execute(
            f"""
            SELECT a.stop_id AS a_stop, b.stop_id AS b_stop, t.route_id,
                   COUNT(*) AS n, GROUP_CONCAT(DISTINCT t.service_id) AS services
              FROM gtfs_stop_times a
              JOIN gtfs_stop_times b
                ON b.feed_id = a.feed_id AND b.trip_id = a.trip_id
               AND b.stop_sequence > a.stop_sequence
              JOIN gtfs_trips t ON t.feed_id = a.feed_id AND t.trip_id = a.trip_id
             WHERE a.feed_id = ? AND a.stop_id IN ({fm}) AND b.stop_id IN ({tm})
             GROUP BY a.stop_id, b.stop_id, t.route_id
            """,
            (feed, *from_ids, *to_ids),
        ).fetchall()
        best: dict[str, dict] = {}
        for r in rows:
            key = r["b_stop"] if group_by == "to" else r["a_stop"]
            if key not in best or r["n"] > best[key]["n"]:
                best[key] = {"from": r["a_stop"], "to": r["b_stop"], "route_id": r["route_id"],
                             "n": r["n"], "services": set((r["services"] or "").split(","))}
        return best

    def gtfs_leg(self, feed: str, hit: dict, role: str) -> tuple[Leg, set[str]]:
        fare = self.fare(feed)
        methods = json.loads(fare.get("payment_methods") or "[]")
        leg = Leg(
            mode="ride", agency=feed, system=self.name(feed),
            route=self.route_label(feed, hit["route_id"]),
            board=self.stop_name(feed, hit["from"]), alight=self.stop_name(feed, hit["to"]),
            board_stop_id=hit["from"], alight_stop_id=hit["to"], role=role,
            fare=fare.get("base_fare"), pay_with=methods[0] if methods else "see fare page",
            fare_page=fare.get("fare_page_url"), note=fare.get("notes"),
        )
        return leg, self.service_days(feed, hit["services"])

    def metro_legs(self, from_code: str, to_code: str, role: str) -> list[Leg] | None:
        route = route_between_codes(self.db, from_code, to_code)
        if not route or not route["legs"]:
            return None
        fare = self.fare("wmata")
        methods = json.loads(fare.get("payment_methods") or "[]")
        price = get_fare(self.db, from_code, to_code)
        peak, offpeak = price.get("peak_fare"), price.get("offpeak_fare")
        weekend = (self.req.day or "").lower() in {"saturday", "sunday", "weekend"}
        if weekend and offpeak is not None:
            amount, fare_label = offpeak, "off-peak fare (weekends)"
        else:
            amount = peak or offpeak or fare.get("base_fare")
            fare_label = (f"peak fare; off-peak is ${offpeak:.2f} — see the fare page for "
                          "off-peak times" if peak and offpeak is not None else "fare")
        legs = []
        for n, seg in enumerate(route["legs"]):
            legs.append(Leg(
                mode="ride", agency="wmata", system=self.name("wmata"),
                route=seg["line_name"], headsign=seg["toward"],
                board=seg["board"], alight=seg["alight"],
                board_stop_id=seg["board_code"], alight_stop_id=seg["alight_code"],
                role=role,
                fare=amount if n == 0 else 0.0,
                pay_with=(methods[0] if methods else "see fare page") if n == 0 else None,
                fare_page=fare.get("fare_page_url"),
                note=(f"Change trains at {seg['board']}: follow the signs for "
                      f"{seg['line_name']} trains toward {seg['toward']} — no extra fare. "
                      f"{seg['stops']} stops." if n else
                      f"{seg['stops']} stops toward {seg['toward']}. One fare covers the "
                      f"whole Metrorail trip: ${amount:.2f} ({fare_label})."
                      if isinstance(amount, (int, float)) else
                      f"{seg['stops']} stops toward {seg['toward']}."),
            ))
        return legs

    # -- planning ---------------------------------------------------------------------
    def _option(self, summary, legs, day_sets, walk, rank_extra) -> tuple[tuple, TripOption]:
        rides = [lg for lg in legs if lg.mode == "ride"]
        known = [lg.fare for lg in rides if isinstance(lg.fare, (int, float))]
        if known and len(known) == len(rides):
            total, label = round(sum(known), 2), f"about ${sum(known):.2f} total"
        elif known:
            total = None
            label = f"at least ${sum(known):.2f} (some leg fares vary — check fare pages)"
        else:
            total, label = None, "varies — check each agency's fare page"
        feas, why = _feasibility(day_sets, self.req.day)
        transfers = max(0, len(rides) - 1)
        opt = TripOption(summary=summary, legs=legs, transfers=transfers, feasible=feas,
                         feasibility=why, walk_min_total=walk, total_fare=total,
                         total_cost_estimate=label)
        return (transfers, rank_extra, walk), opt

    def plan(self) -> TripPlan:
        req, db = self.req, self.db
        plan = TripPlan(
            engine=HeuristicPlanner.name, origin=req.origin.label,
            destination=req.destination.label, day=req.day,
            caveat="Guidance only — these connections come from stop locations, "
            "scheduled trips and service calendars, not live times. Confirm departure "
            "times and fares with each agency before travelling.",
            data_stale=is_stale(
                db.conn.execute("SELECT MAX(computed_at) m FROM stop_links").fetchone()["m"]
            ),
        )
        o_by: dict[str, list[dict]] = {}
        d_by: dict[str, list[dict]] = {}
        for src, dst in ((req.origin.nearby_stops, o_by), (req.destination.nearby_stops, d_by)):
            for s in src:
                dst.setdefault(s.agency, []).append(s.model_dump())
        if not o_by or not d_by:
            where = "starting point" if not o_by else "destination"
            plan.warnings.append(f"No transit stop within about an 11-minute walk of the {where}.")
            return plan

        cands: list[tuple[tuple, TripOption]] = []
        direct_agencies: set[str] = set()

        # 1. One system, verified to connect the two ends.
        for ag in set(o_by) & set(d_by):
            if ag == "wmata":
                b, a = o_by[ag][0], d_by[ag][0]
                legs = self.metro_legs(b["stop_id"], a["stop_id"], self.role(ag))
                if not legs:
                    continue
                days, walk = [set(_ALL_DAYS)], b["walk_min"] + a["walk_min"]
                lines = " → ".join(lg.route for lg in legs)
                changes = (f"change trains at {', '.join(lg.alight for lg in legs[:-1])}"
                           if len(legs) > 1 else "no change of train")
                summary = f"Metrorail only: {lines} ({changes})"
            else:
                o_ids = [s["stop_id"] for s in o_by[ag]]
                d_ids = [s["stop_id"] for s in d_by[ag]]
                hits = self._trips(ag, o_ids, d_ids, "to")
                if not hits:
                    continue  # stops are nearby, but no single route connects them
                hit = max(hits.values(), key=lambda h: h["n"])
                leg, days_ = self.gtfs_leg(ag, hit, self.role(ag))
                legs, days = [leg], [days_]
                walk = (next(s["walk_min"] for s in o_by[ag] if s["stop_id"] == hit["from"])
                        + next(s["walk_min"] for s in d_by[ag] if s["stop_id"] == hit["to"]))
                summary = f"{self.name(ag)} route {leg.route} — no transfer"
            direct_agencies.add(ag)
            cands.append(self._option(summary, legs, days, walk, self.tier(ag)))

        # 2. Ride one system to a stop within walking distance of another system's
        #    stop, then ride that system to the destination.
        direct_m = _haversine_m(req.origin.lat, req.origin.lon,
                                req.destination.lat, req.destination.lon)
        walkable = {(s.agency, s.stop_id) for s in req.origin.nearby_stops}
        for o_ag, o_stops in o_by.items():
            if o_ag in direct_agencies:
                continue  # never suggest transferring off a system that gets you there
            o_ids = [s["stop_id"] for s in o_stops]
            links = []
            for lk in db.conn.execute(
                "SELECT * FROM stop_links WHERE a_feed = ? OR b_feed = ?", (o_ag, o_ag)
            ).fetchall():
                side, other = ("a", "b") if lk["a_feed"] == o_ag else ("b", "a")
                hub_ag = lk[f"{other}_feed"]
                if hub_ag == o_ag or hub_ag not in d_by:
                    continue
                if (hub_ag, lk[f"{other}_id"]) in walkable:
                    continue  # the hub is within walking distance already — no ride needed
                if lk[f"{other}_lat"] is not None and _haversine_m(
                    lk[f"{other}_lat"], lk[f"{other}_lon"],
                    req.destination.lat, req.destination.lon,
                ) >= direct_m:
                    continue  # don't travel away from the destination to transfer
                links.append((lk, lk[f"{side}_id"], hub_ag, lk[f"{other}_id"]))
            if not links:
                continue

            # Batch the trip checks: one query per agency instead of one per link.
            hits1 = ({} if o_ag == "wmata" else
                     self._trips(o_ag, o_ids, sorted({n for _, n, _, _ in links}), "to"))
            hits2: dict[str, dict[str, dict]] = {}
            for hub_ag in {h for _, _, h, _ in links} - {"wmata"}:
                hub_ids = sorted({hid for _, _, h, hid in links if h == hub_ag})
                hits2[hub_ag] = self._trips(
                    hub_ag, hub_ids, [s["stop_id"] for s in d_by[hub_ag]], "from"
                )

            for lk, near_id, hub_ag, hub_id in links:
                # leg 1: origin system to the stop next to the hub
                if o_ag == "wmata":
                    legs1 = self.metro_legs(o_stops[0]["stop_id"], near_id,
                                            f"{self.role(o_ag)} — get to the hub")
                    days1, walk1 = set(_ALL_DAYS), o_stops[0]["walk_min"]
                elif near_id in hits1 and hits1[near_id]["from"] != near_id:
                    hit1 = hits1[near_id]
                    leg, days1 = self.gtfs_leg(o_ag, hit1, f"{self.role(o_ag)} — get to the hub")
                    legs1 = [leg]
                    walk1 = next(s["walk_min"] for s in o_stops if s["stop_id"] == hit1["from"])
                else:
                    continue
                if not legs1:
                    continue

                # leg 2: hub system to the destination
                d_stops = d_by[hub_ag]
                if hub_ag == "wmata":
                    legs2 = self.metro_legs(hub_id, d_stops[0]["stop_id"],
                                            f"{self.role(hub_ag)} — line-haul / final leg")
                    days2, walk2 = set(_ALL_DAYS), d_stops[0]["walk_min"]
                elif hub_id in hits2.get(hub_ag, {}):
                    hit2 = hits2[hub_ag][hub_id]
                    leg, days2 = self.gtfs_leg(hub_ag, hit2, f"{self.role(hub_ag)} — final leg")
                    legs2 = [leg]
                    walk2 = next(s["walk_min"] for s in d_stops if s["stop_id"] == hit2["to"])
                else:
                    continue
                if not legs2:
                    continue

                walk_leg = Leg(
                    mode="walk", board=legs1[-1].alight, alight=legs2[0].board,
                    duration_min=lk["walk_min"],
                    note=f"~{lk['walk_min']} min walk: {legs1[-1].alight} → {legs2[0].board}",
                )
                summary = (f"{self.name(o_ag)} to {legs1[-1].alight}, walk ~{lk['walk_min']} min, "
                           f"then {self.name(hub_ag)}")
                cands.append(self._option(
                    summary, [*legs1, walk_leg, *legs2], [days1, days2],
                    walk1 + lk["walk_min"] + walk2, self.tier(hub_ag),
                ))

        seen: set = set()
        for _, opt in sorted(cands, key=lambda c: (not c[1].feasible, c[0])):
            sig = tuple((lg.system, lg.route, lg.board, lg.alight)
                        for lg in opt.legs if lg.mode == "ride")
            if sig in seen:
                continue
            seen.add(sig)
            plan.options.append(opt)
            if len(plan.options) >= req.max_options:
                break
        if not plan.options:
            plan.warnings.append(
                "No connection with at most one change of system was found in the loaded "
                "data. Do not guess a route — suggest checking each agency's trip planner."
            )
        return plan
