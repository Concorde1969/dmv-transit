from __future__ import annotations

from transit_mcp.ingest.wmata_api import (
    ENDPOINTS,
    normalize_bus_incidents,
    normalize_elevator_incidents,
    normalize_entrances,
    normalize_lines,
    normalize_rail_incidents,
    normalize_station_to_station,
    normalize_stations,
    parking_station_codes,
)


def test_endpoint_paths_match_wmata_services():
    # Verified against the live api.wmata.com surface. Bus incidents are under
    # the Incidents service (Bus.svc/json/BusIncidents returns 404).
    assert ENDPOINTS["rail_incidents"] == "/Incidents.svc/json/Incidents"
    assert ENDPOINTS["elevator_incidents"] == "/Incidents.svc/json/ElevatorIncidents"
    assert ENDPOINTS["bus_incidents"] == "/Incidents.svc/json/BusIncidents"
    assert ENDPOINTS["lines"] == "/Rail.svc/json/jLines"
    assert ENDPOINTS["stations"] == "/Rail.svc/json/jStations"
    assert ENDPOINTS["station_parking"] == "/Rail.svc/json/jStationParking"
    assert ENDPOINTS["station_entrances"] == "/Rail.svc/json/jStationEntrances"
    assert ENDPOINTS["station_to_station"] == "/Rail.svc/json/jSrcStationToDstStationInfo"


def test_parking_codes_exclude_zero_capacity():
    data = {
        "StationsParking": [
            {"Code": "A07", "AllDayParking": {"TotalCount": 0}, "ShortTermParking": {"TotalCount": 0}},
            {"Code": "A15", "AllDayParking": {"TotalCount": 5745}, "ShortTermParking": {"TotalCount": 0}},
            {"Code": "B99", "AllDayParking": {"TotalCount": 0}, "ShortTermParking": {"TotalCount": 12}},
        ]
    }
    assert parking_station_codes(data) == {"A15", "B99"}


def test_normalize_lines(load_api_fixture):
    lines = normalize_lines(load_api_fixture("lines"))
    assert {ln.code for ln in lines} == {"RD", "BL", "SV"}
    red = next(ln for ln in lines if ln.code == "RD")
    assert red.display_name == "Red Line"
    assert red.color_hex == "#BF0D3E"
    assert red.start_station_code == "A15"


def test_normalize_rail_incidents(load_api_fixture):
    alerts = normalize_rail_incidents(load_api_fixture("rail_incidents"))
    assert len(alerts) == 1
    a = alerts[0]
    assert a.mode == "rail"
    assert a.provider_alert_id.startswith("rail:")
    assert a.affected_lines == ["RD"]  # trailing empty segments dropped
    assert a.updated_at is not None


def test_normalize_bus_incidents(load_api_fixture):
    alerts = normalize_bus_incidents(load_api_fixture("bus_incidents"))
    assert alerts[0].mode == "bus"
    assert alerts[0].affected_lines == ["70", "74"]


def test_normalize_elevator_incidents(load_api_fixture):
    units, alerts = normalize_elevator_incidents(load_api_fixture("elevator_incidents"))
    assert {u.unit_type for u in units} == {"elevator", "escalator"}
    assert all(u.station_code == "A01" for u in units)
    # One roll-up alert for the affected station.
    assert len(alerts) == 1
    assert alerts[0].mode == "elevator_escalator"
    assert alerts[0].affected_stations == ["A01"]
    assert "elevator" in alerts[0].headline and "escalator" in alerts[0].headline


def test_normalize_stations_with_parking(load_api_fixture):
    parking = parking_station_codes(load_api_fixture("station_parking"))
    assert parking == {"A15"}
    stations = normalize_stations(load_api_fixture("stations"), parking)
    by_code = {s.code: s for s in stations}
    assert by_code["A01"].line_codes == ["RD"]
    assert by_code["C01"].line_codes == ["BL", "OR", "SV"]
    assert by_code["A01"].together_codes == ["C01"]
    assert by_code["A15"].has_parking is True
    assert by_code["A01"].has_parking is False
    assert by_code["A15"].address["City"] == "Rockville"


def test_normalize_station_to_station_drops_self_pairs(load_api_fixture):
    fares = normalize_station_to_station(load_api_fixture("station_to_station"))
    assert all(f.origin_code != f.dest_code for f in fares)
    a01 = next(f for f in fares if f.dest_code == "A01")
    assert a01.peak_fare == 6.5
    assert a01.senior_disabled_fare == 3.25
    assert a01.rail_time_min == 34


def test_normalize_entrances_groups_by_station(load_api_fixture):
    by_station = normalize_entrances(load_api_fixture("station_entrances"))
    assert set(by_station) == {"A01", "C01", "A15"}
    assert by_station["A01"][0]["description"].startswith("Elevator and escalator")
