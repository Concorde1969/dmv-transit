"""Disruptions on trips, GTFS-realtime alerts, web-page pruning, and fare checks."""

from __future__ import annotations

import time

from transit_mcp.ingest.sources.gtfs_rt_alerts import parse_alerts
from transit_mcp.models import Alert, Document
from transit_mcp.retrieval.disruptions import TripScope, for_trip
from transit_mcp.retrieval.queries import wmata_fare_check, wmata_rail_fare_ranges


def _feed_message(entities) -> bytes:
    from google.transit import gtfs_realtime_pb2

    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = int(time.time())
    for eid, header, route, stop, period in entities:
        ent = msg.entity.add()
        ent.id = eid
        ent.alert.effect = 4  # DETOUR
        ent.alert.header_text.translation.add(text=header, language="en")
        informed = ent.alert.informed_entity.add()
        informed.agency_id = "1"
        if route:
            informed.route_id = route
        if stop:
            informed.stop_id = stop
        if period:
            p = ent.alert.active_period.add()
            p.start, p.end = period
    return msg.SerializeToString()


def test_gtfs_rt_alerts_parse_routes_stops_and_skip_future_ones():
    now = int(time.time())
    content = _feed_message([
        ("1", "Route 401 detour", "401", "", None),
        ("2", "Stop 1792 closed", "231", "1792", (now - 60, now + 3600)),
        ("3", "Next month: route 950 change", "950", "", (now + 86400 * 30, 0)),
    ])
    alerts = parse_alerts(content, feed_id="fairfax-connector", mode="bus")
    assert [a.provider_alert_id for a in alerts] == [
        "gtfsrt:fairfax-connector:1", "gtfsrt:fairfax-connector:2"]
    assert alerts[1].affected_routes == ["231"] and alerts[1].affected_stops == ["1792"]
    assert alerts[0].severity == "detour"


def _alert(db, pid, feed, mode, headline, **kw):
    db.upsert_alert(source_id=None, alert=Alert(
        provider_alert_id=pid, feed_id=feed, mode=mode, headline=headline, raw_json="{}",
        **kw))


def test_for_trip_matches_lines_routes_stops_and_filters_noise(tmp_db):
    _alert(tmp_db, "rail:1", "wmata", "rail", "Blue Line delays", affected_lines=["BL"])
    _alert(tmp_db, "rail:2", "wmata", "rail", "Red Line delays", affected_lines=["RD"])
    _alert(tmp_db, "elev:C05", "wmata", "elevator_escalator", "1 elevator out",
           affected_stations=["C05"])
    _alert(tmp_db, "gtfsrt:ffx:1", "fairfax-connector", "bus", "Route 952 missed trip",
           affected_routes=["952"])
    _alert(tmp_db, "gtfsrt:ffx:2", "fairfax-connector", "bus", "Route 321LOOP - Missed Trip")
    _alert(tmp_db, "gtfsrt:ffx:3", "fairfax-connector", "bus", "Snow schedule today")
    _alert(tmp_db, "bus:9", "wmata", "bus", "A11 detour", affected_lines=["A11"])
    tmp_db.commit()

    scope = TripScope()
    scope.add("wmata", "BL", ["C10", "C05"])
    scope.add("fairfax-connector", "952", ["s1"])
    scope.add("metrobus", "A11", ["b1"])
    out = for_trip(tmp_db, scope)
    got = {a["headline"]: a["affects"] for a in out["active_disruptions"]}
    assert got == {
        "Blue Line delays": "BL",
        "1 elevator out": "stop/station C05",
        "Route 952 missed trip": "952",
        "Snow schedule today": "the whole system",   # system-wide, kept
        "A11 detour": "A11",                          # Metrobus via WMATA API incidents
    }
    assert "Tell the rider" in out["disruption_note"]


def _web_doc(db, url, feed="wmata", category="alerts", body="text " * 50):
    sid, _ = db.upsert_source(url=url, source_type="web", category=category, feed_id=feed,
                              http_status=200, content_hash=url)
    from datetime import datetime

    db.replace_document(source_id=sid, document=Document(
        url=url, category=category, feed_id=feed, title=url.rsplit("/", 1)[-1],
        summary=body[:100], body_markdown=body, raw_hash=url, retrieved_at=datetime.now(),
    ), chunks=[])


def test_prune_drops_pages_the_crawl_no_longer_reaches(tmp_db):
    for u in ("https://w/pay.html", "https://w/maps.html", "https://w/old-alert.html"):
        _web_doc(tmp_db, u)
    _web_doc(tmp_db, "https://other/fares", feed="dash")
    n = tmp_db.prune_web_documents(feed_id="wmata", keep_urls={"https://w/pay.html"})
    assert n == 2
    urls = {r[0] for r in tmp_db.conn.execute("SELECT url FROM documents")}
    assert urls == {"https://w/pay.html", "https://other/fares"}


PAY_PAGE = """# Pay for Metro

### Metro Rail

| Fare Type | Price |
|---|---|
| Weekday (5 am-9:30 pm) $2.25 - $6.75 |  |
| Weekday (5 am-9:30 pm, Reduced Fare) $1.10 - $3.35 |  |
| Weekend $2.25 - $2.50 |  |

### Metro Bus

| Express routes $4.80 |  |
"""


def test_wmata_fare_check_uses_the_fare_page_ranges(tmp_db):
    _web_doc(tmp_db, "https://www.wmata.com/pay.html", category="fares", body=PAY_PAGE)
    assert wmata_rail_fare_ranges(tmp_db)["regular"] == (2.25, 6.75)
    assert wmata_rail_fare_ranges(tmp_db)["reduced"] == (1.10, 3.35)
    assert wmata_fare_check(tmp_db, 6.75, 2.50, 3.35) is None
    warning = wmata_fare_check(tmp_db, 7.25, 2.50, 3.35)
    assert "peak $7.25" in warning and "wmata.com/pay.html" in warning


def test_dated_only_services_get_their_days(tmp_db):
    """WMATA's feeds use all-zero calendar rows plus calendar_dates."""
    from transit_mcp.ingest.gtfs.summarize import _service_day_masks

    tmp_db.conn.execute("INSERT INTO gtfs_calendar (feed_id, service_id) VALUES ('f', '1')")
    tmp_db.conn.execute(
        "INSERT INTO gtfs_calendar (feed_id, service_id, sunday) VALUES ('f', 'SUN', 1)")
    tmp_db.conn.executemany(
        "INSERT INTO gtfs_calendar_dates (feed_id, service_id, date, exception_type) "
        "VALUES ('f', ?, ?, 1)",
        [("1", "20261026"), ("1", "20261027"),   # Mon, Tue
         ("SUN", "20261012")],                    # holiday Monday on Sunday service
    )
    masks = _service_day_masks(tmp_db, "f")
    assert masks["1"] == [1, 1, 0, 0, 0, 0, 0]
    assert masks["SUN"] == [0, 0, 0, 0, 0, 0, 1]  # holiday swap doesn't add Monday
