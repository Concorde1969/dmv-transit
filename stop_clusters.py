"""
GTFS stop clustering for transit connection analysis (DC / Northern Virginia).

Pulls stops for each feed from the Transitland v2 REST API, groups stops that are
within MAX_DIST_KM of each other into clusters, and writes:
  - stops_all.json          every stop pulled (feed, id, name, lat, lon)
  - stop_clusters.json      clusters, sorted with multi-agency clusters first
  - stop_clusters_map.html  interactive map: every stop as a point, colored by feed

Clustering method
-----------------
Plain "link anything within 3 km" (single linkage) chains stops together across the
whole region, so a dense bus network collapses into one giant cluster. Instead this
uses a greedy radius cover: the stop with the most unassigned neighbours becomes a
cluster centre, and every unassigned stop within MAX_DIST_KM / 2 of it joins. By the
triangle inequality every pair of stops in a cluster is then < MAX_DIST_KM apart.
Repeat until all stops are assigned.

Requires: pip install requests numpy scikit-learn folium
"""

import heapq
import json
import os
import time
from collections import Counter

import folium
import numpy as np
import requests
from sklearn.metrics.pairwise import haversine_distances
from sklearn.neighbors import BallTree

# --------------------------------------------------------------------------- config
API_KEY = os.environ.get(
    "TRANSITLAND_API_KEY",
    "iwa_live_tlv2api_7cda1feae73bd9232e67841f7019e69229ff07cfd885b9a74pxNsw",
)
HEADERS = {"apikey": API_KEY}
STOPS_URL = "https://transit.land/api/v2/rest/stops"

FEEDS = [
    "f-dqc-wmata~rail",
    "f-dqc-wmata~bus",
    "f-dqc-virginiarailwayexpress",
    "f-dqcj-fairfaxconnector",
    "f-dqcjj-arlingtontransit",
    "f-dqchv-alexandriatransitcompanydash",
    "f-dqcj1-fairfaxcue",
    "f-loudouncounty~va~us",
    "f-dqch-prtc",
    "f-utel~uiuc~intercity~bus",
    "f-virginia~breeze",
]

MAX_DIST_KM = 0.3          # every pair of stops in a cluster is closer than this
EARTH_RADIUS_KM = 6371.0088
PAGE_LIMIT = 100
OUT_DIR = "."


# --------------------------------------------------------------------------- fetch
def get_with_retry(url, params=None, tries=6):
    for attempt in range(tries):
        r = requests.get(url, headers=HEADERS, params=params, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            wait = 2 ** attempt
            print(f"  HTTP {r.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Gave up on {url}")


def fetch_stops(feed_key):
    """All stops for one feed, following Transitland's cursor pagination."""
    stops = []
    url, params = STOPS_URL, {"feed_onestop_id": feed_key, "limit": PAGE_LIMIT}
    while url:
        data = get_with_retry(url, params)
        stops.extend(data.get("stops", []))
        url = data.get("meta", {}).get("next")   # full URL incl. "after" cursor
        params = None
        time.sleep(0.5)                          # stay polite with rate limits
    return stops


def normalise(feed_key, s):
    """Keep boardable locations only; return a flat record or None."""
    loc_type = s.get("location_type") or 0
    # 0 = stop/platform, 1 = station. Drop entrances (2), nodes (3), boarding areas (4).
    if loc_type not in (0, 1):
        return None
    # Platforms that belong to a station are represented by the station itself.
    if loc_type == 0 and s.get("parent"):
        return None
    geom = s.get("geometry") or {}
    coords = geom.get("coordinates")
    if not coords or len(coords) < 2:
        return None
    lon, lat = coords[0], coords[1]
    return {
        "feed": feed_key,
        "stop_id": s.get("stop_id"),
        "onestop_id": s.get("onestop_id"),
        "name": s.get("stop_name"),
        "lat": lat,
        "lon": lon,
        "location_type": loc_type,
    }


# --------------------------------------------------------------------------- cluster
def cluster_stops(stops, max_dist_km=MAX_DIST_KM):
    coords = np.radians([[s["lat"], s["lon"]] for s in stops])
    tree = BallTree(coords, metric="haversine")
    radius = (max_dist_km / 2) / EARTH_RADIUS_KM
    neighbours = tree.query_radius(coords, r=radius)

    # Lazy max-heap of "how many unassigned stops would this centre absorb".
    # Counts are decremented as stops get assigned, so total work ~ number of
    # neighbour pairs (fast even for 15k+ regional bus stops).
    n = len(stops)
    counts = np.array([len(nb) for nb in neighbours])
    unassigned = np.ones(n, dtype=bool)
    labels = np.full(n, -1)
    heap = [(-int(c), i) for i, c in enumerate(counts)]
    heapq.heapify(heap)
    cid = 0
    while heap:
        negc, i = heapq.heappop(heap)
        if not unassigned[i]:
            continue
        if -negc != counts[i]:              # stale entry, re-queue with current count
            heapq.heappush(heap, (-int(counts[i]), i))
            continue
        members = neighbours[i][unassigned[neighbours[i]]]
        labels[members] = cid
        unassigned[members] = False
        for mbr in members:                 # those stops no longer count for anyone
            counts[neighbours[mbr]] -= 1
        cid += 1
    return labels


def build_cluster_records(stops, labels):
    clusters = {}
    for s, lab in zip(stops, labels):
        clusters.setdefault(int(lab), []).append(s)

    records = []
    for cid, members in clusters.items():
        feeds = Counter(m["feed"] for m in members)
        lat = float(np.mean([m["lat"] for m in members]))
        lon = float(np.mean([m["lon"] for m in members]))
        max_pair = 0.0
        if len(members) > 1:
            rad = np.radians([[m["lat"], m["lon"]] for m in members])
            max_pair = float(haversine_distances(rad).max() * EARTH_RADIUS_KM)
        records.append({
            "cluster_id": cid,
            "centroid": {"lat": lat, "lon": lon},
            "n_stops": len(members),
            "n_feeds": len(feeds),
            "feeds": dict(feeds),
            "cross_agency": len(feeds) > 1,
            "max_pairwise_km": round(max_pair, 3),
            "stops": members,
        })
    # multi-agency hubs first, then biggest
    records.sort(key=lambda r: (-r["n_feeds"], -r["n_stops"]))
    for s, lab in zip(stops, labels):
        s["cluster_id"] = int(lab)
    return records


# --------------------------------------------------------------------------- map
PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4",
           "#f032e6", "#9a6324", "#000075", "#808000", "#469990", "#800000"]


def make_map(stops, clusters, path):
    lat0 = float(np.mean([s["lat"] for s in stops]))
    lon0 = float(np.mean([s["lon"] for s in stops]))
    m = folium.Map(location=[lat0, lon0], zoom_start=10, tiles="OpenStreetMap",
                   prefer_canvas=True)

    # Multi-agency clusters as translucent circles underneath the stops
    hubs = folium.FeatureGroup(name="Cross-agency clusters", show=True)
    for c in clusters:
        if not c["cross_agency"]:
            continue
        folium.Circle(
            location=[c["centroid"]["lat"], c["centroid"]["lon"]],
            radius=MAX_DIST_KM / 2 * 1000,
            color="#555", weight=1, fill=True, fill_opacity=0.08,
            tooltip=f"Cluster {c['cluster_id']}: {c['n_stops']} stops, "
                    f"{c['n_feeds']} feeds",
        ).add_to(hubs)
    hubs.add_to(m)

    feeds = sorted({s["feed"] for s in stops})
    colours = {f: PALETTE[i % len(PALETTE)] for i, f in enumerate(feeds)}
    for f in feeds:
        fg = folium.FeatureGroup(name=f"{f} ({sum(s['feed'] == f for s in stops)})")
        rail = "rail" in f or "railway" in f
        for s in stops:
            if s["feed"] != f:
                continue
            folium.CircleMarker(
                location=[s["lat"], s["lon"]],
                radius=6 if rail else 3,
                color=colours[f], fill=True, fill_opacity=0.8, weight=1,
                popup=folium.Popup(
                    f"<b>{s['name']}</b><br>{f}<br>stop_id: {s['stop_id']}"
                    f"<br>cluster: {s['cluster_id']}", max_width=250),
            ).add_to(fg)
        fg.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    m.save(path)


# --------------------------------------------------------------------------- main
def main():
    all_stops = []
    for feed in FEEDS:
        print(f"Fetching {feed} ...")
        try:
            raw = fetch_stops(feed)
        except Exception as e:  # keep going if one feed fails
            print(f"  FAILED: {e}")
            continue
        kept = [r for r in (normalise(feed, s) for s in raw) if r]
        print(f"  {len(raw)} raw stops, {len(kept)} kept")
        if not raw:
            print("  (no stops returned - check the feed key on transit.land)")
        all_stops.extend(kept)

    if not all_stops:
        raise SystemExit("No stops fetched.")

    labels = cluster_stops(all_stops)
    clusters = build_cluster_records(all_stops, labels)

    with open(os.path.join(OUT_DIR, "stops_all.json"), "w") as f:
        json.dump(all_stops, f, indent=2)
    with open(os.path.join(OUT_DIR, "stop_clusters.json"), "w") as f:
        json.dump({
            "max_pairwise_distance_km": MAX_DIST_KM,
            "method": "greedy radius cover (radius = max_dist/2)",
            "n_stops": len(all_stops),
            "n_clusters": len(clusters),
            "n_cross_agency_clusters": sum(c["cross_agency"] for c in clusters),
            "clusters": clusters,
        }, f, indent=2)
    make_map(all_stops, clusters, os.path.join(OUT_DIR, "stop_clusters_map.html"))

    print(f"\n{len(all_stops)} stops -> {len(clusters)} clusters, "
          f"{sum(c['cross_agency'] for c in clusters)} cross-agency")
    print("Top cross-agency clusters:")
    for c in clusters[:10]:
        names = sorted({s['name'] for s in c['stops'] if 'rail' in s['feed']})[:3]
        print(f"  #{c['cluster_id']:>4}  {c['n_feeds']} feeds  {c['n_stops']:>4} stops  {names}")


if __name__ == "__main__":
    main()