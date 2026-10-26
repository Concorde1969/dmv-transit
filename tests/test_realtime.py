from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2

from transit_mcp.planning.realtime import (
    CombinedRealtime,
    DirectTripUpdates,
    parse_trip_updates,
)

TZ = ZoneInfo("America/New_York")
DAY = date(2026, 10, 26)
MIDNIGHT = int(datetime.combine(DAY, datetime.min.time(), TZ).timestamp())


def _feed() -> bytes:
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    e = msg.entity.add(id="1")
    e.trip_update.trip.trip_id = "timed"
    u = e.trip_update.stop_time_update.add(stop_id="A", stop_sequence=1)
    u.departure.time = MIDNIGHT + 8 * 3600 + 120
    e = msg.entity.add(id="2")
    e.trip_update.trip.trip_id = "delayed"
    u = e.trip_update.stop_time_update.add(stop_id="A", stop_sequence=1)
    u.departure.delay = 240
    e = msg.entity.add(id="3")
    e.trip_update.trip.trip_id = "gone"
    e.trip_update.trip.schedule_relationship = gtfs_realtime_pb2.TripDescriptor.CANCELED
    e = msg.entity.add(id="4")
    e.trip_update.trip.trip_id = "skip"
    u = e.trip_update.stop_time_update.add(stop_id="B", stop_sequence=2)
    u.schedule_relationship = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED
    return msg.SerializeToString()


def _client(content=None, status=200):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=content or b"")
    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_parse_trip_updates():
    trips = parse_trip_updates(_feed())
    assert trips["timed"]["stops"][0]["time"] == MIDNIGHT + 8 * 3600 + 120
    assert trips["delayed"]["stops"][0]["delay"] == 240
    assert trips["gone"]["cancelled"]
    assert trips["skip"]["stops"][0]["skipped"]


def test_direct_lookup_exact_delay_forward_and_cancelled():
    http, calls = _client(_feed())
    rt = DirectTripUpdates({"bus": ("https://x/tu", {"api_key": "k"})}, http=http)
    sched = 8 * 3600
    info, n = rt.lookup("bus", "A", "timed", 1, sched, DAY)
    assert info["dep_s"] == sched + 120 and info["live"] and n == 0
    # Delay carried forward to a later stop the feed doesn't list.
    info, _ = rt.lookup("bus", "Z", "delayed", 5, sched, DAY)
    assert info["dep_s"] == sched + 240 and info["delay"] == 240
    assert rt.lookup("bus", "A", "gone", 1, sched, DAY)[0]["cancelled"]
    assert rt.lookup("bus", "B", "skip", 2, sched, DAY)[0]["cancelled"]
    assert rt.lookup("bus", "A", "unknown", 1, sched, DAY)[0] is None
    assert len(calls) == 1 and calls[0].headers["api_key"] == "k"   # one cached download


class _Fallback:
    def __init__(self):
        self.asked = []

    def available(self):
        return True

    def free(self, feed, stop):
        return False

    def lookup(self, feed, stop_id, trip_id, seq, sched, day):
        self.asked.append(feed)
        return {"dep_s": 1, "delay": None, "live": True, "cancelled": False}, 1


def test_combined_falls_back_when_direct_feed_fails():
    http, _ = _client(status=503)
    fallback = _Fallback()
    rt = CombinedRealtime(DirectTripUpdates({"bus": ("https://x/tu", {})}, http=http),
                          fallback)
    assert rt.free("bus", "A")
    info, calls = rt.lookup("bus", "A", "t", 1, 100, DAY)
    assert info["dep_s"] == 1 and calls == 1 and fallback.asked == ["bus"]
    assert not rt.free("bus", "A")          # failed feed skipped for a while
    rt.lookup("rail", "A", "t", 1, 100, DAY)  # no direct feed: Transitland
    assert fallback.asked == ["bus", "rail"]
