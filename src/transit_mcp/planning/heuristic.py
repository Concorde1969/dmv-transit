"""Built-in planner: a feasibility-and-cost connection advisor.

Not a time-routed journey planner. It finds which systems serve each end, picks
good transfer hubs (frequent systems such as Metro first), states which days the
connection actually works, and adds up the fare for each leg with how to pay it.
This is the default engine and the fallback when an external engine is unavailable.
"""

from __future__ import annotations

import json

from transit_mcp.db.database import Database
from transit_mcp.planning.models import Leg, TripOption, TripPlan, TripRequest
from transit_mcp.retrieval.queries import get_fare, is_stale
from transit_mcp.retrieval.regional import (
    _DAY_COLS,
    _DAY_FULL,
    _day_filter,
    _haversine_m,
    _stop_service,
)

_TIER_RANK = {"frequent": 0, "peak": 1, "intercity": 2, "infrequent": 3, None: 4}
_ALL_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri"}
_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class HeuristicPlanner:
    name = "heuristic"

    def __init__(self, db_path):
        self._db_path = db_path

    def describe(self) -> dict:
        return {
            "name": self.name,
            "version": "1",
            "kind": "built-in",
            "capabilities": ["feasibility_by_day", "fares", "one_transfer"],
            "notes": "Connection advisor from stop proximity and service calendars; "
            "does not compute exact departure times.",
        }

    def plan(self, request: TripRequest) -> TripPlan:
        with Database(self._db_path) as db:
            return _plan(db, request)


# ---------------------------------------------------------------------------
def _agency_meta(db: Database) -> dict[str, dict]:
    meta: dict[str, dict] = {}
    for table, key in (("agency_profiles", "profile"), ("fare_vending", "fare"),
                       ("gtfs_feeds", "feed")):
        for r in db.conn.execute(f"SELECT * FROM {table}"):
            meta.setdefault(r["feed_id"], {})[key] = dict(r)
    return meta


def _name(meta: dict, ag: str) -> str:
    m = meta.get(ag, {})
    return (m.get("profile", {}).get("display_name")
            or m.get("feed", {}).get("agency_name") or ag)


def _role(meta: dict, ag: str) -> str:
    return meta.get(ag, {}).get("profile", {}).get("role", "") or ""


def _tier(meta: dict, ag: str) -> str | None:
    return meta.get(ag, {}).get("profile", {}).get("frequency_tier")


def _days_on(db: Database, stop: dict) -> set[str]:
    if stop["kind"] == "wmata":
        return set(_ALL_DAYS)
    svc = _stop_service(db, stop["agency"], stop["stop_id"])
    if not svc:
        return set(_ALL_DAYS)  # unknown -> don't over-constrain
    return {d[5:] for d in _DAY_COLS if svc[d]}


def _feasibility(day_sets: list[set[str]], day: str | None) -> tuple[bool, str]:
    common = set(_ALL_DAYS)
    for s in day_sets:
        common &= s
    if not common:
        return False, "the legs have no day of the week in common"
    if common == _ALL_DAYS:
        label = "every day"
    elif common == _WEEKDAYS:
        label = "Mon–Fri"
    elif common == {"sat", "sun"}:
        label = "weekends"
    else:
        label = ", ".join(_DAY_FULL[d] for d in _ORDER if d in common)
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


def _ride(db: Database, meta: dict, board: dict, alight: dict, role: str) -> Leg:
    ag = board["agency"]
    fare = meta.get(ag, {}).get("fare", {})
    methods = json.loads(fare.get("payment_methods") or "[]")
    amount = fare.get("base_fare")
    if ag == "wmata" and board["kind"] == "wmata" and alight["kind"] == "wmata":
        f = get_fare(db, board["stop_id"], alight["stop_id"])  # real distance fare
        amount = f.get("peak_fare") or f.get("offpeak_fare") or amount
    return Leg(
        mode="ride", agency=ag, system=_name(meta, ag),
        board=board["name"], alight=alight["name"],
        board_stop_id=board["stop_id"], alight_stop_id=alight["stop_id"],
        role=role, fare=amount,
        pay_with=methods[0] if methods else "see fare page",
        fare_page=fare.get("fare_page_url"), note=fare.get("notes"),
    )


def _totals(legs: list[Leg]) -> tuple[float | None, str]:
    rides = [lg for lg in legs if lg.mode == "ride"]
    known = [lg.fare for lg in rides if isinstance(lg.fare, (int, float))]
    if known and len(known) == len(rides):
        return round(sum(known), 2), f"about ${sum(known):.2f} total"
    if known:
        return None, (f"at least ${sum(known):.2f} (some leg fares vary — "
                      "check fare pages)")
    return None, "varies — check each agency's fare page"


def _best_per_agency(stops) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in stops:
        d = s.model_dump() if hasattr(s, "model_dump") else dict(s)
        cur = out.get(d["agency"])
        if cur is None or d["distance_m"] < cur["distance_m"]:
            out[d["agency"]] = d
    return out


def _plan(db: Database, req: TripRequest) -> TripPlan:
    meta = _agency_meta(db)
    o_near = _best_per_agency(req.origin.nearby_stops)
    d_near = _best_per_agency(req.destination.nearby_stops)
    plan = TripPlan(
        engine=HeuristicPlanner.name,
        origin=req.origin.label,
        destination=req.destination.label,
        day=req.day,
        caveat="Guidance only — these are plausible connections from stop locations "
        "and service calendars, not verified schedules. Confirm departure times and "
        "fares with each agency before travelling.",
        data_stale=is_stale(
            db.conn.execute("SELECT MAX(computed_at) m FROM stop_links").fetchone()["m"]
        ),
    )
    if not o_near or not d_near:
        where = "starting point" if not o_near else "destination"
        plan.warnings.append(
            f"No transit stop within about an 11-minute walk of the {where}."
        )
        return plan

    candidates: list[tuple[tuple, TripOption]] = []

    # 1. One system serves both ends.
    for ag in set(o_near) & set(d_near):
        board, alight = o_near[ag], d_near[ag]
        if board["stop_id"] == alight["stop_id"]:
            continue
        legs = [_ride(db, meta, board, alight, _role(meta, ag))]
        feas, why = _feasibility([_days_on(db, board), _days_on(db, alight)], req.day)
        total, label = _totals(legs)
        walk = board["walk_min"] + alight["walk_min"]
        candidates.append(((0, _TIER_RANK.get(_tier(meta, ag), 4), walk), TripOption(
            summary=f"{_name(meta, ag)} only — no transfer", legs=legs, transfers=0,
            feasible=feas, feasibility=why, walk_min_total=walk,
            total_fare=total, total_cost_estimate=label,
        )))

    # 2. Ride an origin-side system to a stop that links (on foot) to a system that
    #    serves the destination, then ride that system. Skip systems that already
    #    reach the destination themselves — never suggest transferring off them.
    direct_m = _haversine_m(
        req.origin.lat, req.origin.lon, req.destination.lat, req.destination.lon
    )
    for o_ag, o_stop in o_near.items():
        if o_ag in d_near:
            continue
        for lk in db.conn.execute(
            "SELECT * FROM stop_links WHERE a_feed = ? OR b_feed = ?", (o_ag, o_ag)
        ).fetchall():
            side, other = ("a", "b") if lk["a_feed"] == o_ag else ("b", "a")
            near_hub = {"agency": o_ag, "stop_id": lk[f"{side}_id"],
                        "name": lk[f"{side}_name"], "kind": lk[f"{side}_kind"]}
            hub = {"agency": lk[f"{other}_feed"], "stop_id": lk[f"{other}_id"],
                   "name": lk[f"{other}_name"], "kind": lk[f"{other}_kind"]}
            hub_ag = hub["agency"]
            if hub_ag == o_ag or hub_ag not in d_near:
                continue
            # Don't travel away from the destination just to transfer.
            hub_lat, hub_lon = lk[f"{other}_lat"], lk[f"{other}_lon"]
            if hub_lat is not None and _haversine_m(
                hub_lat, hub_lon, req.destination.lat, req.destination.lon
            ) >= direct_m:
                continue
            d_stop = d_near[hub_ag]
            if d_stop["stop_id"] == hub["stop_id"]:
                continue
            legs = [
                _ride(db, meta, o_stop, near_hub,
                      f"{_role(meta, o_ag)} — get to the {_name(meta, hub_ag)} hub"),
                Leg(mode="walk", board=near_hub["name"], alight=hub["name"],
                    duration_min=lk["walk_min"],
                    note=f"~{lk['walk_min']} min walk: {near_hub['name']} → {hub['name']}"),
                _ride(db, meta, hub, d_stop,
                      f"{_role(meta, hub_ag)} — line-haul / final leg"),
            ]
            feas, why = _feasibility(
                [_days_on(db, o_stop), _days_on(db, near_hub),
                 _days_on(db, hub), _days_on(db, d_stop)],
                req.day,
            )
            total, label = _totals(legs)
            walk = o_stop["walk_min"] + lk["walk_min"] + d_stop["walk_min"]
            candidates.append(((1, _TIER_RANK.get(_tier(meta, hub_ag), 4), walk), TripOption(
                summary=f"{_name(meta, o_ag)} to {near_hub['name']}, walk "
                f"~{lk['walk_min']} min to {hub['name']}, then {_name(meta, hub_ag)}",
                legs=legs, transfers=1, feasible=feas, feasibility=why,
                walk_min_total=walk, total_fare=total, total_cost_estimate=label,
            )))

    seen: set = set()
    for _, opt in sorted(candidates, key=lambda c: (not c[1].feasible, c[0])):
        sig = tuple((lg.system, lg.board, lg.alight) for lg in opt.legs if lg.mode == "ride")
        if sig in seen:
            continue
        seen.add(sig)
        plan.options.append(opt)
        if len(plan.options) >= req.max_options:
            break

    if not plan.options:
        plan.warnings.append(
            "No single- or one-transfer connection found in the loaded data. Try Metro "
            "plus a local bus, or check each agency directly."
        )
    return plan
