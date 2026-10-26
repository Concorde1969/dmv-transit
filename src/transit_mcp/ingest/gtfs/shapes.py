"""Route shapes from ``shapes.txt``, simplified and stored compactly for drawing maps.

Raw shapes are most of a GTFS feed's size (WMATA bus: ~18 MB), so each shape is
simplified with Douglas-Peucker (~10 m tolerance) and stored as an encoded polyline
(Google's format, 5 decimal places) in ``gtfs_shapes``.
"""

from __future__ import annotations

import csv
import io
import math
import zipfile

from transit_mcp.db.database import Database

TOLERANCE_M = 10.0
_M_PER_DEG = 111_320.0


def simplify(points: list[tuple[float, float]], tolerance_m: float = TOLERANCE_M
             ) -> list[tuple[float, float]]:
    """Douglas-Peucker on (lat, lon) points, using a local equirectangular projection."""
    if len(points) < 3:
        return list(points)
    k = math.cos(math.radians(points[0][0]))
    xy = [(lon * _M_PER_DEG * k, lat * _M_PER_DEG) for lat, lon in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        worst, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            if seg2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > worst:
                worst, idx = d, i
        if worst > tolerance_m and idx > 0:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k_ in zip(points, keep) if k_]


def encode(points: list[tuple[float, float]]) -> str:
    """Google encoded polyline (precision 5)."""
    out, prev_lat, prev_lon = [], 0, 0
    for lat, lon in points:
        ilat, ilon = round(lat * 1e5), round(lon * 1e5)
        for v in (ilat - prev_lat, ilon - prev_lon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def decode(text: str) -> list[tuple[float, float]]:
    points, i, lat, lon = [], 0, 0, 0
    while i < len(text):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(text[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        points.append((lat / 1e5, lon / 1e5))
    return points


def load_shapes(db: Database, feed_id: str, zf: zipfile.ZipFile) -> int:
    """Replace this feed's shapes with simplified, encoded copies. Returns shapes stored."""
    db.conn.execute("DELETE FROM gtfs_shapes WHERE feed_id = ?", (feed_id,))
    if "shapes.txt" not in zf.namelist():
        return 0
    # Stream one shape at a time (shapes.txt is normally grouped by shape_id) so a
    # national feed with millions of points never sits in memory at once. A shape whose
    # points come back later is merged with the simplified copy already kept.
    done: dict[str, list[tuple[int, float, float]]] = {}
    current_id: str | None = None
    current: list[tuple[int, float, float]] = []

    def finish(shape_id: str | None, raw_pts: list[tuple[int, float, float]]) -> None:
        if shape_id is None or not raw_pts:
            return
        raw_pts = sorted(done.pop(shape_id, []) + raw_pts)
        kept = simplify([(lat, lon) for _, lat, lon in raw_pts])
        keep = set(kept)
        done[shape_id] = [p for p in raw_pts if (p[1], p[2]) in keep]

    with zf.open("shapes.txt") as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        header = [h.strip() for h in next(reader, [])]
        try:
            i_id, i_lat, i_lon, i_seq = (header.index(c) for c in (
                "shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"))
        except ValueError:
            return 0
        for row in reader:
            try:
                sid = row[i_id].strip()
                pt = (int(float(row[i_seq])), float(row[i_lat]), float(row[i_lon]))
            except (ValueError, IndexError):
                continue
            if sid != current_id:
                finish(current_id, current)
                current_id, current = sid, []
            current.append(pt)
        finish(current_id, current)

    db.conn.executemany(
        "INSERT INTO gtfs_shapes (feed_id, shape_id, polyline, n_points) VALUES (?, ?, ?, ?)",
        [(feed_id, sid, encode([(lat, lon) for _, lat, lon in pts]), len(pts))
         for sid, pts in done.items()],
    )
    return len(done)
