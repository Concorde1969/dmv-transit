"""Ride lines that reach their stops, and walking the whole way on short trips."""

from __future__ import annotations

from transit_mcp.chat.trip_api import WALK_ONLY_MAX_S, add_walk_option
from transit_mcp.ingest.gtfs.clusters import haversine_m
from transit_mcp.planning.live import LiveSettings, Point, slice_shape


def _gap(pt_lonlat, stop_latlon):
    return haversine_m(pt_lonlat[1], pt_lonlat[0], stop_latlon[0], stop_latlon[1])


def test_ride_line_starts_and_ends_at_the_stops_on_a_long_straight_street():
    # A simplified shape: a straight street 2 km long (no points in between), then a turn.
    shape = [(38.8800, -77.0000), (38.8800, -76.9770), (38.8900, -76.9770)]
    board, alight = (38.8801, -76.9950), (38.8801, -76.9850)   # both mid-street
    line = slice_shape(shape, [board, alight])
    assert line is not None
    assert _gap(line[0], board) < 20 and _gap(line[-1], alight) < 20


def test_ride_line_follows_the_turn_between_stops():
    shape = [(38.8800, -77.0000), (38.8800, -76.9770), (38.8900, -76.9770)]
    board, alight = (38.8800, -76.9900), (38.8850, -76.9771)
    line = slice_shape(shape, [board, alight])
    assert [-76.977, 38.88] in line                       # the corner is kept
    assert _gap(line[-1], alight) < 20


def test_a_shape_that_misses_the_stops_is_not_used():
    shape = [(38.9000, -77.0000), (38.9000, -76.9800)]
    assert slice_shape(shape, [(38.8800, -76.9950), (38.8800, -76.9850)]) is None


def _ride_option(arrive_at):
    return {"arrive_at": arrive_at, "legs": [{"mode": "ride"}]}


def test_short_trip_offers_walking_first_when_it_is_about_as_quick():
    cfg = LiveSettings()
    a, b = Point(38.88660, -76.99635, "Eastern Market"), Point(38.88700, -77.00360, "LoC")
    result = {"options": [_ride_option(1000 + 13 * 60)]}
    add_walk_option(result, a, b, cfg, now=1000)
    walk = result["options"][0]
    assert walk["walk_only"] and walk["legs"] == [{"mode": "walk", "duration_s": walk["walk_s"]}]
    assert walk["arrive_at"] == 1000 + walk["walk_s"]


def test_walking_goes_last_when_riding_is_much_quicker():
    cfg = LiveSettings()
    a, b = Point(38.880, -77.000, "A"), Point(38.890, -77.000, "B")      # about 1.1 km
    result = {"options": [_ride_option(1000 + 5 * 60)]}
    add_walk_option(result, a, b, cfg, now=1000)
    assert result["options"][-1]["walk_only"]


def test_no_walking_option_for_a_long_trip_and_none_found_is_cleared():
    cfg = LiveSettings()
    far = {"options": []}
    add_walk_option(far, Point(38.88, -77.0, "A"), Point(38.95, -77.0, "B"), cfg, now=0)
    assert far["options"] == []
    near = {"options": [], "notice": "No trips found."}
    add_walk_option(near, Point(38.880, -77.0, "A"), Point(38.882, -77.0, "B"), cfg, now=0)
    assert near["options"][0]["walk_only"] and near["notice"] is None
    assert near["options"][0]["walk_s"] <= WALK_ONLY_MAX_S


def test_rides_that_need_as_much_walking_as_walking_there_are_dropped():
    cfg = LiveSettings()
    a, b = Point(38.88660, -76.99635, "Eastern Market"), Point(38.88700, -77.00360, "LoC")
    keep = {**_ride_option(1000 + 13 * 60), "walk_s": 8 * 60}
    pointless = {**_ride_option(1000 + 18 * 60), "walk_s": 17 * 60}
    result = {"options": [keep, pointless]}
    add_walk_option(result, a, b, cfg, now=1000)
    assert result["options"][0]["walk_only"] and result["options"][1] is keep
    assert len(result["options"]) == 2
