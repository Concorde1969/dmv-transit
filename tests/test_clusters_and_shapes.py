"""Stop clustering (every pair < 0.3 km), shape simplification/encoding, API budget."""

from __future__ import annotations

import io
import random
import zipfile

import pytest

from transit_mcp.api_budget import ApiBudget
from transit_mcp.ingest.gtfs.clusters import build_clusters, cluster_points, haversine_m
from transit_mcp.ingest.gtfs.shapes import decode, encode, load_shapes, simplify


def test_every_pair_in_a_cluster_is_within_300_m():
    rng = random.Random(7)
    pts = [(38.88 + rng.uniform(0, 0.03), -77.03 + rng.uniform(0, 0.04)) for _ in range(800)]
    labels = cluster_points(pts)
    groups: dict[int, list] = {}
    for p, lab in zip(pts, labels):
        groups.setdefault(lab, []).append(p)
    assert -1 not in labels
    for ms in groups.values():
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                assert haversine_m(*ms[i], *ms[j]) < 300


def test_greedy_cover_groups_a_hub_and_separates_far_stops():
    hub = [(38.8960, -77.0718), (38.8962, -77.0716), (38.8958, -77.0720)]  # ~40 m apart
    far = [(38.9200, -77.0400)]
    labels = cluster_points(hub + far)
    assert len(set(labels[:3])) == 1 and labels[3] != labels[0]


def test_build_clusters_folds_platforms_into_their_station(tmp_db):
    rows = [
        ("rail", "STN_C05", "Rosslyn", 38.8960, -77.0718, 1, None),
        ("rail", "PF_C05_1", "Rosslyn platform", 38.8961, -77.0719, 0, "STN_C05"),
        ("bus", "b1", "N Moore St", 38.8964, -77.0712, 0, None),
        ("bus", "b2", "far away", 38.9500, -77.0100, 0, None),
    ]
    tmp_db.conn.executemany(
        "INSERT INTO gtfs_stops (feed_id, stop_id, name, lat, lon, location_type, "
        "parent_station) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    stats = build_clusters(tmp_db)
    cluster_of = {(r["feed_id"], r["stop_id"]): r["cluster_id"]
                  for r in tmp_db.conn.execute("SELECT * FROM cluster_stops")}
    assert cluster_of[("rail", "PF_C05_1")] == cluster_of[("rail", "STN_C05")]
    assert cluster_of[("bus", "b1")] == cluster_of[("rail", "STN_C05")]
    assert cluster_of[("bus", "b2")] != cluster_of[("bus", "b1")]
    assert stats == {"stops": 3, "clusters": 2, "cross_agency": 1}


def test_polyline_round_trip_and_simplify():
    line = [(38.9 + i * 1e-4, -77.0) for i in range(50)]          # straight: 2 points
    assert simplify(line) == [line[0], line[-1]]
    bent = [(38.9, -77.0), (38.901, -77.0), (38.901, -76.998)]    # corner kept
    assert len(simplify(bent)) == 3
    pts = [(38.89512, -77.03653), (38.90001, -77.04), (38.88, -77.1)]
    assert decode(encode(pts)) == pytest.approx(pts, abs=1e-5)


def test_load_shapes_stores_simplified_encoded_lines(tmp_db):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        lines = ["shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence"]
        lines += [f"s1,{38.9 + i * 1e-4:.6f},-77.0,{i}" for i in range(30)]
        lines += ["s2,38.9,-77.0,2", "s2,38.8,-77.0,1"]  # out of order on purpose
        zf.writestr("shapes.txt", "\n".join(lines))
    n = load_shapes(tmp_db, "f", zipfile.ZipFile(buf))
    rows = {r["shape_id"]: r for r in tmp_db.conn.execute("SELECT * FROM gtfs_shapes")}
    assert n == 2 and rows["s1"]["n_points"] == 2
    assert decode(rows["s2"]["polyline"]) == [(38.8, -77.0), (38.9, -77.0)]


def test_api_budget(tmp_db):
    b = ApiBudget(tmp_db, service="transitland", monthly_limit=3)
    assert b.try_spend(2) and b.used() == 2
    assert not b.try_spend(2) and b.used() == 2   # would exceed: nothing reserved
    assert b.try_spend() and b.remaining() == 0
    assert not b.try_spend()
