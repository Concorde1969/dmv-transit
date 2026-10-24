"""Build the cross-system transfer graph (stop_links).

A link is any two stops close enough to walk between, from different agencies
(GTFS feeds or WMATA). Each link caches a coarse service-overlap label so the
advisor can quickly reason about whether a transfer is possible on a given day.
"""

from __future__ import annotations

import math
from datetime import datetime

from transit_mcp.db.database import Database
from transit_mcp.ingest.gtfs.summarize import _days_label

CELL = 0.0025  # ~275 m of latitude; bucket size for the proximity scan
_ALL_DAYS = [1] * 7  # WMATA rail/bus effectively runs every day


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _overlap(mask_a, mask_b) -> tuple[str | None, int]:
    if mask_a is None or mask_b is None:
        return None, 0
    inter = [x & y for x, y in zip(mask_a, mask_b)]
    if not any(inter):
        return "no overlapping days of service", 0
    return _days_label(inter), int(bool(inter[5] or inter[6]))


def _load_nodes(db: Database) -> list[dict]:
    nodes: list[dict] = []
    for r in db.conn.execute(
        """
        SELECT feed_id, stop_id, name, lat, lon
          FROM gtfs_stops
         WHERE lat IS NOT NULL AND lon IS NOT NULL
           AND (location_type IS NULL OR location_type IN (0, ''))
           -- e.g. Metrorail platforms: linked by station code instead
           AND feed_id NOT IN (SELECT feed_id FROM gtfs_feeds WHERE index_stops = 0)
        """
    ):
        nodes.append({
            "kind": "gtfs", "feed": r["feed_id"], "id": r["stop_id"],
            "name": r["name"], "lat": r["lat"], "lon": r["lon"],
        })
    for r in db.conn.execute(
        "SELECT code, name, lat, lon FROM stations WHERE lat IS NOT NULL"
    ):
        nodes.append({
            "kind": "wmata", "feed": "wmata", "id": r["code"],
            "name": r["name"], "lat": r["lat"], "lon": r["lon"],
        })
    return nodes


def _day_masks(db: Database) -> dict[tuple[str, str], list[int]]:
    masks: dict[tuple[str, str], list[int]] = {}
    for r in db.conn.execute(
        """
        SELECT feed_id, stop_id, runs_mon, runs_tue, runs_wed, runs_thu,
               runs_fri, runs_sat, runs_sun
          FROM stop_service_summary
        """
    ):
        masks[(r["feed_id"], r["stop_id"])] = [
            r["runs_mon"], r["runs_tue"], r["runs_wed"], r["runs_thu"],
            r["runs_fri"], r["runs_sat"], r["runs_sun"],
        ]
    return masks


def build_stop_links(
    db: Database, *, radius_m: int = 250, walk_speed_m_per_min: float = 80.0
) -> dict:
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    db.conn.execute("DELETE FROM stop_links")

    nodes = _load_nodes(db)
    masks = _day_masks(db)

    buckets: dict[tuple[int, int], list[int]] = {}
    for i, n in enumerate(nodes):
        key = (int(n["lat"] / CELL), int(n["lon"] / CELL))
        buckets.setdefault(key, []).append(i)

    def mask_of(node: dict):
        if node["kind"] == "wmata":
            return _ALL_DAYS
        return masks.get((node["feed"], node["id"]))

    seen: set[tuple] = set()
    links = 0
    for (cx, cy), idxs in buckets.items():
        neigh: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neigh.extend(buckets.get((cx + dx, cy + dy), ()))
        for i in idxs:
            a = nodes[i]
            for j in neigh:
                if j <= i:
                    continue
                b = nodes[j]
                if a["feed"] == b["feed"]:
                    continue
                dist = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
                if dist > radius_m:
                    continue
                # Canonical orientation so each unordered pair is stored once.
                first, second = (
                    (a, b) if (a["feed"], a["id"]) < (b["feed"], b["id"]) else (b, a)
                )
                pair = (first["feed"], first["id"], second["feed"], second["id"])
                if pair in seen:
                    continue
                seen.add(pair)
                label, weekend = _overlap(mask_of(first), mask_of(second))
                db.conn.execute(
                    """
                    INSERT INTO stop_links
                        (a_kind, a_feed, a_id, a_name, a_lat, a_lon,
                         b_kind, b_feed, b_id, b_name, b_lat, b_lon,
                         distance_m, walk_min, link_source, overlap_days,
                         both_serve_weekend, computed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proximity', ?, ?, ?)
                    ON CONFLICT(a_feed, a_id, b_feed, b_id) DO UPDATE SET
                        distance_m = excluded.distance_m,
                        walk_min = excluded.walk_min,
                        overlap_days = excluded.overlap_days,
                        both_serve_weekend = excluded.both_serve_weekend,
                        computed_at = excluded.computed_at
                    """,
                    (
                        first["kind"], first["feed"], first["id"], first["name"],
                        first["lat"], first["lon"],
                        second["kind"], second["feed"], second["id"], second["name"],
                        second["lat"], second["lon"],
                        round(dist), max(1, round(dist / walk_speed_m_per_min)),
                        label, weekend, now,
                    ),
                )
                links += 1

    transfers = _import_feed_transfers(db, now)
    db.commit()
    return {"nodes": len(nodes), "links": links, "gtfs_transfers": transfers}


def _import_feed_transfers(db: Database, now: str) -> int:
    n = 0
    rows = db.conn.execute(
        "SELECT feed_id, from_stop_id, to_stop_id, min_transfer_time "
        "FROM gtfs_transfers WHERE from_stop_id IS NOT NULL AND to_stop_id IS NOT NULL "
        "AND from_stop_id <> to_stop_id "
        "AND feed_id NOT IN (SELECT feed_id FROM gtfs_feeds WHERE index_stops = 0)"
    ).fetchall()
    for r in rows:
        s = {
            x["stop_id"]: x
            for x in db.conn.execute(
                "SELECT stop_id, name, lat, lon FROM gtfs_stops WHERE feed_id=? "
                "AND stop_id IN (?, ?)",
                (r["feed_id"], r["from_stop_id"], r["to_stop_id"]),
            )
        }
        a, b = s.get(r["from_stop_id"]), s.get(r["to_stop_id"])
        if not a or not b:
            continue
        dist = (
            _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            if a["lat"] and b["lat"]
            else 0
        )
        db.conn.execute(
            """
            INSERT INTO stop_links
                (a_kind, a_feed, a_id, a_name, a_lat, a_lon,
                 b_kind, b_feed, b_id, b_name, b_lat, b_lon,
                 distance_m, walk_min, link_source, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'gtfs_transfers', ?)
            ON CONFLICT(a_feed, a_id, b_feed, b_id) DO UPDATE SET
                link_source = 'gtfs_transfers', computed_at = excluded.computed_at
            """,
            (
                "gtfs", r["feed_id"], r["from_stop_id"], a["name"], a["lat"], a["lon"],
                "gtfs", r["feed_id"], r["to_stop_id"], b["name"], b["lat"], b["lon"],
                round(dist),
                max(1, round((r["min_transfer_time"] or 120) / 60)),
                now,
            ),
        )
        n += 1
    return n
