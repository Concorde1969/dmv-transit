"""Stop clusters: groups of stops close enough to walk between to change vehicles.

A port of the clustering in the user's Transitland stop-connector script
(``tools/transitland_stop_clusters.py``). Linking "anything within 0.3 km" chains
stops across a whole city, so instead it uses a **greedy radius cover**: the stop with
the most unassigned neighbours within ``max_dist_km / 2`` becomes a cluster centre and
takes all of them. By the triangle inequality every pair of stops in a cluster is then
closer than ``max_dist_km``. Two routes can connect where they serve stops in the same
cluster.

Neighbour search uses a simple lat/lon grid (cells of ``radius`` metres) instead of
scikit-learn's BallTree, so it runs anywhere the app runs.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from datetime import datetime

from transit_mcp.db.database import Database

EARTH_RADIUS_M = 6_371_008.8
MAX_DIST_KM = 0.3


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def cluster_points(points: list[tuple[float, float]], max_dist_km: float = MAX_DIST_KM
                   ) -> list[int]:
    """Cluster label for each (lat, lon) point; every pair in a cluster is < max_dist_km."""
    n = len(points)
    if n == 0:
        return []
    radius = max_dist_km * 1000 / 2
    cell_lat = radius / 111_320.0
    mean_lat = sum(p[0] for p in points) / n
    cell_lon = radius / (111_320.0 * max(math.cos(math.radians(mean_lat)), 0.01))

    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (lat, lon) in enumerate(points):
        grid[(int(lat // cell_lat), int(lon // cell_lon))].append(i)

    neighbours: list[list[int]] = []
    for lat, lon in points:
        gy, gx = int(lat // cell_lat), int(lon // cell_lon)
        near = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for j in grid.get((gy + dy, gx + dx), ()):
                    if haversine_m(lat, lon, points[j][0], points[j][1]) <= radius:
                        near.append(j)
        neighbours.append(near)

    # Lazy max-heap of "how many unassigned stops would this centre absorb".
    counts = [len(nb) for nb in neighbours]
    unassigned = [True] * n
    labels = [-1] * n
    heap = [(-c, i) for i, c in enumerate(counts)]
    heapq.heapify(heap)
    cid = 0
    while heap:
        negc, i = heapq.heappop(heap)
        if not unassigned[i]:
            continue
        if -negc != counts[i]:  # stale entry: re-queue with the current count
            heapq.heappush(heap, (-counts[i], i))
            continue
        members = [j for j in neighbours[i] if unassigned[j]]
        for j in members:
            labels[j] = cid
            unassigned[j] = False
        for j in members:
            for k in neighbours[j]:
                counts[k] -= 1
        cid += 1
    return labels


def build_clusters(db: Database, max_dist_km: float = MAX_DIST_KM) -> dict:
    """(Re)build ``stop_clusters`` / ``cluster_stops`` from every boardable GTFS stop.

    Stations (location_type 1) are clustered in place of their platforms, as in the
    user's script, so a route listed against a platform lands on its station."""
    rows = db.conn.execute(
        """
        SELECT feed_id, stop_id, lat, lon, location_type, parent_station
          FROM gtfs_stops WHERE lat IS NOT NULL AND lon IS NOT NULL
        """
    ).fetchall()
    stations = {(r["feed_id"], r["stop_id"]) for r in rows if (r["location_type"] or 0) == 1}
    kept = [r for r in rows
            if (r["location_type"] or 0) in (0, 1)
            and not ((r["location_type"] or 0) == 0
                     and (r["feed_id"], r["parent_station"]) in stations)]
    labels = cluster_points([(r["lat"], r["lon"]) for r in kept], max_dist_km)

    members: dict[int, list] = defaultdict(list)
    for r, lab in zip(kept, labels):
        members[lab].append(r)
    # Platforms inherit their station's cluster.
    station_cluster = {(r["feed_id"], r["stop_id"]): lab for r, lab in zip(kept, labels)}
    platform_rows = [(station_cluster[(r["feed_id"], r["parent_station"])],
                      r["feed_id"], r["stop_id"])
                     for r in rows
                     if (r["location_type"] or 0) == 0
                     and (r["feed_id"], r["parent_station"]) in station_cluster
                     and (r["feed_id"], r["parent_station"]) in stations]

    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    db.conn.execute("DELETE FROM stop_clusters")
    db.conn.execute("DELETE FROM cluster_stops")
    db.conn.executemany(
        "INSERT INTO stop_clusters (cluster_id, lat, lon, n_stops, n_feeds, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(cid, sum(m["lat"] for m in ms) / len(ms), sum(m["lon"] for m in ms) / len(ms),
          len(ms), len({m["feed_id"] for m in ms}), now) for cid, ms in members.items()],
    )
    db.conn.executemany(
        "INSERT OR REPLACE INTO cluster_stops (cluster_id, feed_id, stop_id) VALUES (?, ?, ?)",
        [(lab, r["feed_id"], r["stop_id"]) for r, lab in zip(kept, labels)] + platform_rows,
    )
    db.commit()
    return {"stops": len(kept), "clusters": len(members),
            "cross_agency": sum(1 for ms in members.values()
                                if len({m["feed_id"] for m in ms}) > 1)}
