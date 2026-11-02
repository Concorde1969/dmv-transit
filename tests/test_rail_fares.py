"""Station-to-station VRE and MARC fares, and the agencies' published fares."""

from __future__ import annotations

import pytest

from transit_mcp.agencies import load_agencies
from transit_mcp.ingest.gtfs.loader import name_platforms
from transit_mcp.planning.rail_fares import leg_fare, tables

# GTFS stop names as the feeds publish them.
VRE_STOPS = ["Alexandria", "Backlick Road", "Broad Run", "Brooke", "Burke Centre", "Crystal City",
             "Franconia / Springfield", "Fredericksburg", "L Enfant", "Leeland Road", "Lorton",
             "Manassas", "Manassas Park", "Quantico", "Rippon", "Rolling Road", "Spotsylvania",
             "Union Station", "Woodbridge"]
PENN = ["PERRYVILLE MARC", "ABERDEEN MARC nb", "EDGEWOOD MARC sb", "MARTIN AIRPORT MARC nb",
        "PENN STATION MARC sb", "WEST BALTIMORE MARC nb", "HALETHORPE MARC sb",
        "BWI RAIL STATION MARC nb", "ODENTON MARC sb", "BOWIE STATE MARC nb", "SEABROOK MARC sb",
        "NEW CARROLLTON MARC nb", "UNION STATION MARC Washington"]
CAMDEN = ["CAMDEN STATION MARC", "ST DENIS MARC nb", "DORSEY MARC sb", "JESSUP MARC nb",
          "SAVAGE MARC sb", "LAUREL RACETRACK MARC sb", "LAUREL MARC nb", "MUIRKIRK MARC sb",
          "GREENBELT MARC nb", "COLLEGE PARK MARC sb", "RIVERDALE MARC nb",
          "UNION STATION MARC Washington"]
BRUNSWICK = ["MARTINSBURG W VA MARC", "DUFFIELDS W VA MARC eb", "HARPERS FERRY W VA MARC wb",
             "BRUNSWICK MD MARC eb", "POINT OF ROCKS MARC wb", "FREDERICK MARC eb",
             "MONOCACY MARC wb", "DICKERSON MARC eb", "BARNESVILLE MARC wb", "BOYDS MARC eb",
             "GERMANTOWN MARC wb", "METROPOLITAN GROVE MARC eb", "GAITHERSBURG MARC wb",
             "WASHINGTON GROVE MARC eb", "ROCKVILLE MARC wb", "GARRETT PARK MARC eb",
             "KENSINGTON MARC wb", "SILVER SPRING MARC eb", "UNION STATION MARC Washington"]


@pytest.mark.parametrize("board, alight, fare", [
    ("Woodbridge", "Union Station", 10.25),
    ("Union Station", "Woodbridge", 10.25),          # same in both directions
    ("Fredericksburg", "L Enfant", 13.45),
    ("Alexandria", "Union Station", 5.00),
    ("Manassas", "Burke Centre", 5.50),
    ("Lorton", "Rolling Road", 3.90),                # same zone
])
def test_vre_fares_go_by_zone(board, alight, fare):
    assert leg_fare("vre", "Fredericksburg Line", board, alight) == fare


@pytest.mark.parametrize("line, board, alight, fare", [
    ("PENN - WASHINGTON", "BWI RAIL STATION MARC nb", "UNION STATION MARC Washington", 8.00),
    ("PENN - WASHINGTON", "PENN STATION MARC sb", "UNION STATION MARC Washington", 9.00),
    ("PENN - WASHINGTON", "PERRYVILLE MARC", "UNION STATION MARC Washington", 13.00),
    ("CAMDEN - WASHINGTON", "CAMDEN STATION MARC", "UNION STATION MARC Washington", 9.00),
    ("CAMDEN - WASHINGTON", "LAUREL RACETRACK MARC sb", "COLLEGE PARK MARC sb", 6.00),
    ("BRUNSWICK - WASHINGTON", "MARTINSBURG W VA MARC", "UNION STATION MARC Washington", 14.00),
    ("BRUNSWICK - WASHINGTON", "ROCKVILLE MARC eb", "SILVER SPRING MARC eb", 6.00),
    ("MARC BRUNSWICK - WASHINGTON", "FREDERICK MARC eb", "SILVER SPRING MARC eb", 8.00),
])
def test_marc_fares_go_by_station_and_line(line, board, alight, fare):
    assert leg_fare("marc", line, board, alight) == fare


def test_every_station_has_a_fare():
    for a in VRE_STOPS:
        for b in VRE_STOPS:
            if a != b:
                assert leg_fare("vre", None, a, b), (a, b)
    for line, stops in (("PENN", PENN), ("CAMDEN", CAMDEN)):
        for a in stops:
            for b in stops:
                if a != b:
                    assert leg_fare("marc", line, a, b), (a, b)
    for b in BRUNSWICK[1:]:
        if "FREDERICK" not in b and "MONOCACY" not in b:
            assert leg_fare("marc", "BRUNSWICK", BRUNSWICK[0], b), b


def test_pairs_the_chart_leaves_blank_have_no_fare():
    # The Frederick branch and West Virginia: MARC's chart gives no fare between them.
    assert leg_fare("marc", "BRUNSWICK", "FREDERICK MARC eb", "POINT OF ROCKS MARC wb") is None
    assert leg_fare("marc", "BRUNSWICK", "MARTINSBURG W VA MARC", "MONOCACY MARC eb") is None


def test_unknown_stops_and_other_feeds_have_no_station_fare():
    assert leg_fare("vre", None, "Woodbridge", "Station Monitor") is None
    assert leg_fare("vre", None, "Woodbridge", "Woodbridge") is None
    assert leg_fare("marc", "PENN", "PERRYVILLE MARC", "ROCKVILLE MARC eb") is None
    assert leg_fare("marc", "SOMETHING ELSE", "PERRYVILLE MARC", "ABERDEEN MARC nb") is None
    assert leg_fare("marc", None, "PERRYVILLE MARC", "ABERDEEN MARC nb") is None
    assert leg_fare("wmata", "Red", "A", "B") is None
    assert leg_fare("vre", None, None, "Woodbridge") is None


def test_vre_tables_follow_the_charts_own_rules():
    vre = tables()["vre"]
    for name in ("single", "day_pass", "ten_trip", "monthly"):
        assert [len(r) for r in vre[name]] == list(range(1, 10))
    for single, day in zip(vre["single"], vre["day_pass"]):
        assert day == [round(x * 2, 2) for x in single]


def test_marc_charts_are_square_and_almost_symmetric():
    """Two pairs differ by direction in MTA's own charts; any other would be a misread."""
    published = {("Martin State Airport", "New Carrollton"), ("Martinsburg", "Duffields")}
    for line in tables()["marc"].values():
        names, fares = line["stations"], line["fares"]
        assert len(fares) == len(names) and all(len(r) == len(names) for r in fares)
        for i, a in enumerate(names):
            assert fares[i][i] == 0
            for j, b in enumerate(names):
                if i < j and fares[i][j] != fares[j][i]:
                    assert (a, b) in published


@pytest.fixture(scope="module")
def agencies(isolated_settings_module):
    return {a.slug: a for a in load_agencies(isolated_settings_module)}


@pytest.fixture(scope="module")
def isolated_settings_module(tmp_path_factory):
    from transit_mcp.config import Settings

    empty = tmp_path_factory.mktemp("agencies")
    return Settings(_env_file=None, AGENCIES_DIR=str(empty))


@pytest.mark.parametrize("slug, base, reduced", [
    ("art", 2.25, 1.10), ("fairfax-connector", 2.25, 1.10), ("metrobus", 2.25, 1.10),
    ("mta-md-bus", 2.00, 1.00), ("mta-md-metro", 2.00, 1.00), ("mta-md-light-rail", 2.00, 1.00),
    ("annapolis-transit", 2.00, 1.00),
    ("cue", 0, 0), ("dash", 0, 0), ("harbor-connector", 0, 0), ("dulles-shuttles", 0, 0),
    ("charm-city-circulator", 0, 0),
])
def test_published_fares(agencies, slug, base, reduced):
    assert (agencies[slug].fares.base_fare, agencies[slug].fares.reduced_fare) == (base, reduced)


@pytest.mark.parametrize("slug", ["vre", "marc", "mta-md-commuter-bus", "amtrak", "flixbus",
                                  "intercity-bus", "virginia-breeze", "potomac-valley"])
def test_fares_that_depend_on_the_trip_have_no_single_price(agencies, slug):
    assert agencies[slug].fares.base_fare is None and agencies[slug].fares.notes


@pytest.mark.parametrize("slug", ["amtrak", "flixbus", "intercity-bus", "virginia-breeze"])
def test_variable_fares_say_so(agencies, slug):
    assert "vary" in agencies[slug].fares.notes


def test_breeze_sends_riders_to_its_ticket_site(agencies):
    assert agencies["virginia-breeze"].fares.fare_page_url == "https://virginiabreeze.tds.ai/#/"


def test_cue_no_longer_links_to_the_dead_page(agencies):
    assert agencies["cue"].fares.fare_page_url == "https://www.fairfaxva.gov/Services/CUE-Bus"


def test_bare_platform_names_become_their_station(tmp_db):
    rows = [("vre", "S1", "Woodbridge", None), ("vre", "P1", "2", "S1"), ("vre", "P2", "Lead", "S1"),
            ("vre", "P3", "28", "S1"), ("vre", "P4", "3", "missing"),
            ("vre", "P5", "Woodbridge Platform A", "S1"), ("other", "Q1", "2", None)]
    tmp_db.conn.executemany("INSERT INTO gtfs_stops (feed_id, stop_id, name, parent_station) "
                            "VALUES (?, ?, ?, ?)", rows)
    assert name_platforms(tmp_db, "vre") == 3
    names = dict(tmp_db.conn.execute("SELECT stop_id, name FROM gtfs_stops").fetchall())
    assert names["P1"] == names["P2"] == names["P3"] == "Woodbridge"
    assert names["P4"] == "3" and names["P5"] == "Woodbridge Platform A"   # left alone
    assert names["Q1"] == "2"                                             # another feed
    assert name_platforms(tmp_db, "vre") == 0                             # already named
    assert leg_fare("vre", None, names["P1"], "Union Station") == 10.25
