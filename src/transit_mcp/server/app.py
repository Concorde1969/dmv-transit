"""FastMCP server. Tools are thin wrappers over ``transit_mcp.retrieval``.

Every response carries provenance (source URL + ``retrieved_at``) and a ``stale``
flag so the model can caveat old data.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from transit_mcp.config import get_settings
from transit_mcp.db.database import Database
from transit_mcp.retrieval import connect, metro, queries, regional
from transit_mcp.retrieval.search import search

_INSTRUCTIONS = """\
This server answers questions about riding public transit in the Washington, DC region:
WMATA (Metrorail + Metrobus) plus the complementary systems — VRE commuter rail,
Virginia Breeze intercity coach, and the Fairfax Connector, ART, DASH, CUE, Loudoun,
and OmniRide bus networks. WMATA data comes from wmata.com + the WMATA API; the other
agencies come from their GTFS feeds plus scraped fare pages. All data is cached locally.
This is an independent, UNOFFICIAL tool — it is not affiliated with, endorsed by, or
operated by WMATA or any other transit agency.

GROUNDING: answer transit questions ONLY from these tools' results. Never fill gaps from
general knowledge or memory — lines, routes, transfers, fares, hours and stations change.
If the tools don't return it, say the information isn't available here and point to the
agency's official website.

Sources: routes, stops, schedules, service days and hours come ONLY from GTFS schedule
data (found through Transitland) via get_metro_route, plan_connection, get_route and
transfers_at. Agency web pages (search_transit_info, how_to_pay's fare_page_text) are
used only for fares, how to pay/ride, and disruptions. When a result lists
active_disruptions, still give the scheduled route and tell the rider about each one.

For any Metrorail trip between two stations call get_metro_route — it says which line(s)
to ride, where to change trains, which direction, and when trains run. Never describe a
Metrorail trip as direct / one seat / no transfer unless it returns "transfers": 0.
For other WMATA specifics use get_fare / get_service_alerts /
get_elevator_escalator_status / get_station_info. For anything spanning agencies use
list_agencies, find_stops_near, get_route, transfers_at, how_to_pay, plan_connection,
and estimate_trip_cost. Use search_transit_info for "how do I..." questions.

When advising on trips: use Metrorail or VRE for fast line-haul and local/express bus
for coverage and the first/last mile. ALWAYS tell the rider which days and times a
connection actually works (many suburban stops have no weekend or evening service),
and how to pay each leg (each agency vends fares differently — SmarTrip, contactless,
a specific app, exact-change cash, or advance online booking). Cite `url` / `fare_page`
fields and flag any `stale` result as possibly out of date.
"""

mcp = FastMCP("transit-mcp-wmata", instructions=_INSTRUCTIONS)


def _db() -> Database:
    return Database(get_settings().db_path)


@mcp.tool()
def search_transit_info(query: str, category: str | None = None, limit: int = 5) -> list[dict]:
    """Full-text search of the agencies' web pages: fares, how to pay, accessibility,
    safety, rules, new-rider/visitor guides, and service-change notices. NOT a source
    for routes, stops, schedules, hours or service days — use get_metro_route,
    plan_connection, get_route or transfers_at (GTFS schedule data) for those.

    Args:
        query: Natural-language search terms, e.g. "pay with phone" or "bringing a bike".
        category: Optional filter. One of: alerts, fares, payment, accessibility,
            safety, rules, maps, new_rider, visitor, station, line, policy.
        limit: Max results (default 5).
    """
    with _db() as db:
        return [h.as_dict() for h in search(db, query, category=category, limit=limit)]


@mcp.tool()
def get_service_alerts(
    line: str | None = None, station: str | None = None, mode: str | None = None,
    agency: str | None = None,
) -> list[dict]:
    """Live service disruptions: WMATA incidents (Metrorail, Metrobus, elevators) and
    agencies' GTFS-realtime alerts (detours, closed stops, missed trips).

    Args:
        line: Metrorail line code (RD, BL, YL, OR, GR, SV) or bus route number.
        station: Metrorail station name or code.
        mode: "rail", "bus", or "elevator_escalator".
        agency: Agency slug, e.g. "wmata", "metrobus", "fairfax-connector".
    """
    with _db() as db:
        return queries.get_service_alerts(db, line=line, station=station, mode=mode,
                                          agency=agency)


@mcp.tool()
def get_rider_reports(station: str | None = None, line: str | None = None,
                      days: int = 2) -> dict:
    """Recent reports from other RIDERS (crowdsourced on this site): delays, crowding,
    broken elevators, tips. Unverified and unofficial: always present them as "riders
    reported ...", with how long ago, never as agency information, and never let them
    replace the timetable or official alerts.

    Args:
        station: A station or stop name to filter by (matched loosely).
        line: A line or route to filter by, e.g. "Red Line", "D80", "MARC".
        days: How many days back to look (default 2, at most 30).
    """
    from transit_mcp.reports import ReportStore

    settings = get_settings()
    rows = ReportStore.from_settings(settings).search(
        station=station, line=line, days=max(1, min(days, 30)))
    keep = ("reported_at", "minutes_ago", "travel_date", "category", "severity",
            "summary", "station", "line", "origin", "destination", "lines", "stations")
    return {
        "reports": [{k: r[k] for k in keep} for r in rows],
        "note": "Unverified reports from riders using this site, not from any agency. "
                "Say they are rider reports and how long ago they were made; confirm "
                "with the agency.",
        "source": "Rider reports on dmvtransit.com",
    }


@mcp.tool()
def get_elevator_escalator_status(station: str | None = None) -> list[dict]:
    """Elevators and escalators currently out of service, optionally for one station."""
    with _db() as db:
        return queries.get_unit_status(db, station=station, only_out=True)


@mcp.tool()
def get_station_info(station: str) -> dict | None:
    """Lines served, address, parking availability, transfers, and entrances for a
    station (accepts a name like "Gallery Place" or a code like "B01")."""
    with _db() as db:
        return queries.get_station_info(db, station)


@mcp.tool()
def get_fare(origin: str, destination: str, rider_type: str = "regular") -> dict:
    """Station-to-station Metrorail fare.

    Args:
        origin: Origin station name or code.
        destination: Destination station name or code.
        rider_type: "regular"/"peak", "offpeak", or "senior"/"disabled"/"reduced".
    """
    with _db() as db:
        return queries.get_fare(db, origin, destination, rider_type=rider_type)


_trip_tools: dict = {}


def _trip_helpers():
    """Geocoder + live-departures client, created once (they hold caches)."""
    if not _trip_tools:
        from transit_mcp.chat.trip_api import agency_info, build_realtime
        from transit_mcp.geocode import Geocoder

        settings = get_settings()
        agencies = agency_info(settings)
        _trip_tools.update(
            settings=settings, agencies=agencies,
            geocoder=Geocoder.from_settings(settings),
            realtime=build_realtime(settings, agencies))
    return _trip_tools


def _place(db: Database, text: str):
    from transit_mcp.planning.live import Point

    t = text.strip()
    try:
        lat, lon = (float(x) for x in t.split(","))
        return Point(lat, lon, t)
    except ValueError:
        pass
    pt = regional.resolve_point(db, t)          # stations, stops, airports, aliases
    if pt and pt.get("kind") in ("wmata", "gtfs"):
        return Point(pt["lat"], pt["lon"], pt["label"])
    from transit_mcp.geocode import GeocodeError

    try:
        hits = _trip_helpers()["geocoder"].search(t)   # addresses and landmarks
    except GeocodeError:
        hits = []
    if hits:
        return Point(hits[0]["lat"], hits[0]["lon"], hits[0]["label"])
    if pt:
        return Point(pt["lat"], pt["lon"], pt["label"])
    return None


@mcp.tool()
def plan_trip_now(origin: str, destination: str) -> dict:
    """Leave-now trip options with LIVE departures, like the site's map: trips leaving
    in the next 3 hours from each agency's closest stops (up to about a mile's walk; the
    walking time is part of each option), direct trips first and one transfer only when
    needed, and transfers only between stops within 0.3 km. Options with short transfer
    waits (10 minutes or less) come first; a trip with a longer wait is offered only when
    nothing better works. Tell the rider how much walking and waiting each option
    involves. Use this when the rider
    wants to go now; use plan_connection for general "how would I get there" or a later
    day.

    Args:
        origin: An address, landmark, station, or "lat,lon".
        destination: An address, landmark, station, or "lat,lon".
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from transit_mcp.planning.live import LivePlanner, LiveSettings

    h = _trip_helpers()
    settings = h["settings"]
    with _db() as db:
        o, d = _place(db, origin), _place(db, destination)
        if not o or not d:
            return {"error": "could_not_find_place",
                    "origin_found": bool(o), "destination_found": bool(d)}
        result = LivePlanner(db, cfg=LiveSettings.from_settings(settings),
                             realtime=h["realtime"], agencies=h["agencies"],
                             tz=settings.timezone).plan(o, d)
    tz = ZoneInfo(settings.timezone)

    def clock(epoch):
        # "%I" (not the Linux-only "%-I"), then drop the leading zero: "5:04 PM"
        return datetime.fromtimestamp(epoch, tz).strftime("%I:%M %p").lstrip("0") \
            if epoch else None

    def brief(opt):
        steps = []
        for leg in opt["legs"]:
            if leg["mode"] == "walk":
                steps.append(f"walk {round(leg['duration_s'] / 60)} min"
                             + (f" (wait {round(leg['wait_s'] / 60)} min)"
                                if leg.get("transfer") else ""))
            else:
                name = leg["route_short_name"] or leg["route_long_name"] or leg["route_id"]
                steps.append(
                    f"{leg['agency_name']} {name} toward {leg['headsign'] or '?'}: board "
                    f"{leg['board']['name']} at {clock(leg['board']['expected'])}"
                    f"{' (live)' if leg['live'] else ' (scheduled)'}, ride {leg['stops']} "
                    f"stops to {leg['alight']['name']}, arrive {clock(leg['alight']['expected'])}")
        return {"leave_at": clock(opt["leave_at"]), "arrive_at": clock(opt["arrive_at"]),
                "minutes": round(opt["duration_s"] / 60), "transfers": opt["transfers"],
                "wait_minutes": round(opt["wait_s"] / 60),
                "walk_minutes": round(opt["walk_s"] / 60), "live": opt["live"],
                "fare_estimate": opt["fare_estimate"], "steps": steps}

    return {
        "from": o.label, "to": d.label,
        "options": [brief(x) for x in result["options"]],
        "notice": result.get("notice"),
        "note": "Departure times marked (live) include real-time updates; others are the "
                "timetable. Riders can see these options on the site's map.",
        "source": "Agencies' GTFS timetables via Transitland, live departures from "
                  "Transitland",
        "url": "https://www.transit.land/",
    }


@mcp.tool()
def get_metro_route(origin: str, destination: str) -> dict:
    """How to ride Metrorail between two stations: which line(s), where to change
    trains, which direction ("toward ..."), number of stops, and the fare. Use this for
    EVERY Metrorail trip — never assume two stations share a line.

    Args:
        origin: Station name, code (e.g. "C10"), or airport name/code ("DCA", "Dulles").
        destination: Station name, code, or airport name/code.
    """
    with _db() as db:
        return metro.metro_route(db, origin, destination)


@mcp.tool()
def list_topics() -> list[dict]:
    """List content categories with document counts and data freshness."""
    with _db() as db:
        return queries.list_topics(db)


@mcp.tool()
def get_guide(topic: str) -> dict | None:
    """Full text of the primary guide for a topic: new_rider, visitor, payment,
    fares, accessibility, safety, or maps."""
    with _db() as db:
        return queries.get_guide(db, topic)


@mcp.tool()
def list_agencies() -> list[dict]:
    """Every transit system in the region (WMATA + VRE, Virginia Breeze, Fairfax
    Connector, ART, DASH, CUE, Loudoun, OmniRide): mode, role, strengths, service
    area, a one-line fare/payment summary, and data freshness."""
    with _db() as db:
        return regional.list_agencies(db)


@mcp.tool()
def find_stops_near(place: str, radius_m: int = 400, agency: str | None = None) -> dict:
    """Stops and stations within walking distance of a place, across all agencies.

    Args:
        place: A Metro station name, a stop name, or "lat,lon".
        radius_m: Search radius in meters (default 400).
        agency: Optional feed slug to restrict to (e.g. "dash", "vre", "wmata").
    """
    with _db() as db:
        return regional.find_stops_near(db, place, radius_m=radius_m, agency=agency)


@mcp.tool()
def get_route(agency: str, query: str) -> dict:
    """One route from an agency's GTFS schedule: days and hours of service, typical
    frequency, endpoints, fare, and any live disruptions on it.

    Args:
        agency: Agency slug or name (e.g. "metrobus", "wmata" for Metrorail lines,
            "fairfax-connector", "OmniRide", "loudoun").
        query: Route number or name (e.g. "A11", "Blue", "599", "Metro Express").
    """
    with _db() as db:
        return regional.get_route(db, agency, query)


@mcp.tool()
def transfers_at(place: str, day: str | None = None, time: str | None = None) -> dict:
    """Routes from OTHER systems reachable on foot from a station or stop, grouped by
    agency, each with walk time and its service window from the GTFS schedules. Pass
    `day` (e.g. "saturday") and/or `time` ("HH:MM") to get a per-connection
    feasibility check.
    """
    with _db() as db:
        return regional.transfers_at(db, place, day=day, time=time)


@mcp.tool()
def how_to_pay(agency: str) -> dict:
    """How to pay to ride one agency. `fare_page_text` is the agency's own fare page
    (the source for prices, discounts and transfer rules); the other fields are a
    shorter hand-maintained summary that the fare page overrides.

    Args:
        agency: Agency slug or name (e.g. "vre", "dash", "omniride", "wmata",
            "metrobus").
    """
    with _db() as db:
        return regional.how_to_pay(db, agency)


@mcp.tool()
def plan_connection(origin: str, destination: str, when: str | None = None) -> dict:
    """Advise on getting between two places across systems: which system for each
    leg, the transfer hub, WHICH DAYS/TIMES the connection works, and the fare +
    how to pay per leg. Guidance, not a booked itinerary.

    Args:
        origin: Place name, station, or "lat,lon".
        destination: Place name, station, or "lat,lon".
        when: Optional — a day ("sunday", "weekday") to check feasibility against.
    """
    with _db() as db:
        return connect.plan_connection(db, origin, destination, when)


@mcp.tool()
def estimate_trip_cost(
    origin: str, destination: str, when: str | None = None,
    rider_category: str | None = None,
) -> dict:
    """Estimate the total fare for a cross-system trip: per-leg fares, how to pay
    each, and a total — noting each leg is usually a separate payment."""
    with _db() as db:
        return connect.estimate_trip_cost(
            db, origin, destination, when, rider_category
        )


@mcp.resource("transit://region/agencies")
def agencies_resource() -> str:
    """JSON roster of every regional system with role and fare headline."""
    with _db() as db:
        return json.dumps(regional.list_agencies(db), indent=2, default=str)


@mcp.resource("transit://wmata/freshness")
def freshness_resource() -> str:
    """JSON: last ingest per scope, per-category fetch times, and row counts."""
    with _db() as db:
        return json.dumps(queries.freshness_report(db), indent=2, default=str)


@mcp.resource("transit://wmata/guide/{topic}")
def guide_resource(topic: str) -> str:
    """Markdown of the primary guide document for ``topic``."""
    with _db() as db:
        guide = queries.get_guide(db, topic)
    if not guide or guide.get("error"):
        return f"No guide found for topic: {topic}"
    return f"# {guide['title']}\n\nSource: {guide['url']}\n\n{guide['markdown']}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
