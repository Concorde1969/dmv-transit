"""
GTFS stop clustering + route connection search (DC / Northern Virginia).

The site itself builds the same clusters from the GTFS feeds it downloads
(src/transit_mcp/ingest/gtfs/clusters.py) without spending API calls. This script is
the original analysis tool: it pulls stops and routes from the Transitland REST API
and draws them on a map. It uses several hundred API calls per run — mind the
monthly quota (Explorer plan: 5,000).

Run from the project folder, with TRANSITLAND_API_KEY set (it's read from the
environment or the project's .env — never put the key in this file):
    pip install requests numpy scikit-learn folium shapely
    python tools/transitland_stop_clusters.py

Pipeline
--------
1. Pull stops for every feed from the Transitland v2 REST API.
2. Pull routes for every feed, with the stops each route serves and its line shape.
3. Cluster stops so that every pair of stops in a cluster is < MAX_DIST_KM apart.
4. Attach routes to stops and clusters. Two routes that serve stops in the same
   cluster can connect there; every such route pair is recorded with the clusters
   where they meet.
5. Write:
     stops_all.json           every stop pulled, with the routes serving it
     stop_clusters.json       clusters (multi-agency first) with their stops and routes
     route_connections.json   every route, the routes it connects to and where,
                              plus a flat list of connecting route pairs
     stop_clusters_map.html   map: stops as dots, routes as lines (edges),
                              cross-agency clusters as circles

Clustering method
-----------------
Plain "link anything within 3 km" (single linkage) chains stops together across the
whole region, so a dense bus network collapses into one giant cluster. Instead this
uses a greedy radius cover: the stop with the most unassigned neighbours becomes a
cluster centre, and every unassigned stop within MAX_DIST_KM / 2 of it joins. By the
triangle inequality every pair of stops in a cluster is then < MAX_DIST_KM apart.

Requires: pip install requests numpy scikit-learn folium shapely
"""

import heapq
import json
import os
import time
from collections import Counter, defaultdict
from itertools import combinations

import folium
import numpy as np
import requests
from shapely.geometry import mapping, shape
from sklearn.metrics.pairwise import haversine_distances
from sklearn.neighbors import BallTree


# --------------------------------------------------------------------------- config
def _api_key():
    """TRANSITLAND_API_KEY from the environment, else from the project's .env."""
    key = os.environ.get("TRANSITLAND_API_KEY", "").strip()
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not key and os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("TRANSITLAND_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise SystemExit("Set TRANSITLAND_API_KEY (environment or .env) first.")
    return key


HEADERS = {}            # filled in main(), so importing this file needs no key
BASE_URL = "https://transit.land/api/v2/rest"

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
    "f-dq-mtamaryland~marc",
    "f-dq-mtamaryland~commuterbus",
    "f-dq-mtamaryland~subwaylink",
    "f-dq-mtamaryland~bus",
    "f-dq-mtamaryland~raillink",
    "f-dqct-annapolistransit",
    "f-harborconnector~md~us",
    "f-potomac~valley~transit~authority",
    "f-9-flixbus",
    "f-9-amtrak~amtrakcalifornia~amtrakcharteredvehicle"
]

RT_FEEDS = [
    "f-wmata~bus~rt",
    "f-wmata~rail~rt",
    "f-vre~rt",
    "f-fairfaxconnector~rt",
    "f-art~rt",
    "f-dqchv-alexandriatransitcompanydash~rt",
    "f-mtamaryland~marc~train~rt",
    "f-dq-mtamaryland~commuterbus~rt",
    "f-dq-mtamaryland~subwaylink~rt",
    "f-dq-mtamaryland~bus~rt",
    "f-dq-mtamaryland~raillink~rt"
]

MAX_DIST_KM = 0.3          # every pair of stops in a cluster is closer than this
EARTH_RADIUS_KM = 6371.0088
STOP_PAGE_LIMIT = 100
ROUTE_PAGE_LIMIT = 20      # routes carry geometry + stop lists, so keep pages small
SIMPLIFY_DEG = 0.0001      # ~10 m line simplification for the map; 0 to disable
OUT_DIR = "."
SERVE_MAP = True           # open the map via localhost when done (see serve_map)

ROUTE_TYPES = {0: "Tram/Light rail", 1: "Subway/Metro", 2: "Rail", 3: "Bus", 4: "Ferry",
               5: "Cable tram", 6: "Aerial lift", 7: "Funicular", 11: "Trolleybus",
               12: "Monorail"}


# --------------------------------------------------------------------------- fetch
def get_with_retry(url, params=None, tries=6):
    for attempt in range(tries):
        r = requests.get(url, headers=HEADERS, params=params, timeout=120)
        if r.status_code == 429 or r.status_code >= 500:
            wait = 2 ** attempt
            print(f"  HTTP {r.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Gave up on {url}")


def fetch_paged(endpoint, params, key):
    """Every record from a Transitland list endpoint, following cursor pagination."""
    out = []
    url, p = f"{BASE_URL}/{endpoint}", dict(params)
    while url:
        data = get_with_retry(url, p)
        out.extend(data.get(key, []))
        url = (data.get("meta") or {}).get("next")   # full URL incl. "after" cursor
        p = None
        time.sleep(0.5)                              # stay polite with rate limits
    return out


def fetch_stops(feed_key):
    return fetch_paged("stops", {"feed_onestop_id": feed_key,
                                 "limit": STOP_PAGE_LIMIT}, "stops")


def fetch_routes(feed_key):
    return fetch_paged("routes", {"feed_onestop_id": feed_key,
                                  "include_geometry": "true",
                                  "include_stops": "true",
                                  "limit": ROUTE_PAGE_LIMIT}, "routes")


# --------------------------------------------------------------------------- normalise
def normalise_stops(feed_key, raw):
    """
    Keep boardable locations only. Platforms that belong to a station are folded
    into the station; `alias` maps every raw stop (incl. platforms) to the kept stop
    that represents it, so routes listed against platforms land on the station.
    Returns (kept_records, alias) where alias maps (feed, stop_id) and
    ("id", transitland_id) -> (feed, kept stop_id).
    """
    station_ids = {s.get("stop_id") for s in raw if (s.get("location_type") or 0) == 1}
    kept, alias = [], {}
    for s in raw:
        loc_type = s.get("location_type") or 0
        parent = s.get("parent") or {}
        parent_sid = parent.get("stop_id")
        if loc_type in (0, 2, 3, 4) and parent_sid in station_ids:
            target = (feed_key, parent_sid)            # represented by its station
        elif loc_type in (0, 1):
            target = (feed_key, s.get("stop_id"))
        else:
            continue
        alias[(feed_key, s.get("stop_id"))] = target
        if s.get("id") is not None:
            alias[("id", s["id"])] = target
        if target != (feed_key, s.get("stop_id")):
            continue
        coords = (s.get("geometry") or {}).get("coordinates")
        if not coords or len(coords) < 2:
            continue
        kept.append({
            "feed": feed_key,
            "stop_id": s.get("stop_id"),
            "onestop_id": s.get("onestop_id"),
            "name": s.get("stop_name"),
            "lat": coords[1],
            "lon": coords[0],
            "location_type": loc_type,
        })
    return kept, alias


def route_key(feed_key, r):
    return f"{feed_key}:{r.get('route_id')}"


def normalise_route(feed_key, r):
    agency = r.get("agency") or {}
    rtype = r.get("route_type_basic", r.get("route_type"))
    color = (r.get("route_color") or "").strip().lstrip("#")
    return {
        "route_key": route_key(feed_key, r),
        "feed": feed_key,
        "route_id": r.get("route_id"),
        "onestop_id": r.get("onestop_id"),
        "short_name": r.get("route_short_name"),
        "long_name": r.get("route_long_name"),
        "route_type": rtype,
        "route_type_name": ROUTE_TYPES.get(rtype, str(rtype)),
        "color": f"#{color}" if len(color) == 6 else None,
        "agency": agency.get("agency_name"),
    }


def route_label(r):
    name = r["short_name"] or r["long_name"] or r["route_id"]
    return f"{r['agency'] or r['feed']} {name}"


# --------------------------------------------------------------------------- cluster
def cluster_stops(stops, max_dist_km=MAX_DIST_KM):
    coords = np.radians([[s["lat"], s["lon"]] for s in stops])
    tree = BallTree(coords, metric="haversine")
    radius = (max_dist_km / 2) / EARTH_RADIUS_KM
    neighbours = tree.query_radius(coords, r=radius)

    # Lazy max-heap of "how many unassigned stops would this centre absorb".
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
        for mbr in members:
            counts[neighbours[mbr]] -= 1
        cid += 1
    return labels


def build_cluster_records(stops, labels, routes):
    for s, lab in zip(stops, labels):
        s["cluster_id"] = int(lab)
    clusters = defaultdict(list)
    for s in stops:
        clusters[s["cluster_id"]].append(s)

    records = []
    for cid, members in clusters.items():
        feeds = Counter(m["feed"] for m in members)
        rkeys = sorted({rk for m in members for rk in m["routes"]})
        route_agencies = {routes[rk]["agency"] or routes[rk]["feed"] for rk in rkeys}
        rad = np.radians([[m["lat"], m["lon"]] for m in members])
        max_pair = (float(haversine_distances(rad).max() * EARTH_RADIUS_KM)
                    if len(members) > 1 else 0.0)
        records.append({
            "cluster_id": cid,
            "centroid": {"lat": float(np.mean([m["lat"] for m in members])),
                         "lon": float(np.mean([m["lon"] for m in members]))},
            "n_stops": len(members),
            "n_feeds": len(feeds),
            "feeds": dict(feeds),
            "cross_agency": len(feeds) > 1,
            "max_pairwise_km": round(max_pair, 3),
            "n_routes": len(rkeys),
            "n_route_agencies": len(route_agencies),
            "routes": [{k: routes[rk][k] for k in
                        ("route_key", "feed", "agency", "short_name", "long_name",
                         "route_type_name")} for rk in rkeys],
            "stops": members,
        })
    # multi-agency hubs first, then the most routes, then most stops
    records.sort(key=lambda r: (-r["n_feeds"], -r["n_routes"], -r["n_stops"]))
    return records


# --------------------------------------------------------------------------- connections
def find_route_connections(clusters, routes):
    """Two routes connect if they both serve a stop in the same cluster."""
    meet = defaultdict(list)                 # (rk_a, rk_b) -> [cluster_id, ...]
    for c in clusters:
        rkeys = [r["route_key"] for r in c["routes"]]
        for a, b in combinations(rkeys, 2):  # rkeys already sorted, so a < b
            meet[(a, b)].append(c["cluster_id"])

    pairs = []
    adjacency = defaultdict(dict)
    for (a, b), cids in meet.items():
        ra, rb = routes[a], routes[b]
        cross = (ra["agency"] or ra["feed"]) != (rb["agency"] or rb["feed"])
        pairs.append({
            "route_a": a, "route_b": b,
            "label_a": route_label(ra), "label_b": route_label(rb),
            "cross_agency": cross,
            "n_clusters": len(cids),
            "cluster_ids": cids,
        })
        adjacency[a][b] = cids
        adjacency[b][a] = cids
    pairs.sort(key=lambda p: (not p["cross_agency"], -p["n_clusters"]))

    by_route = {}
    for rk, r in routes.items():
        conns = adjacency.get(rk, {})
        by_route[rk] = {
            **r,
            "label": route_label(r),
            "n_stops": r["n_stops"],
            "n_connecting_routes": len(conns),
            "n_cross_agency_connections": sum(
                (routes[o]["agency"] or routes[o]["feed"]) != (r["agency"] or r["feed"])
                for o in conns),
            "connects_to": [{"route_key": o, "label": route_label(routes[o]),
                             "cluster_ids": cids}
                            for o, cids in sorted(conns.items(), key=lambda kv: -len(kv[1]))],
        }
    return by_route, pairs


# --------------------------------------------------------------------------- map
PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4",
           "#f032e6", "#9a6324", "#000075", "#808000", "#469990", "#800000"]


def add_basemaps(m):
    """
    Basemaps, switchable in the layer control (first one is shown by default).
    - CARTO Positron: clean light map that keeps transit lines readable. Since Aug 2026
      CARTO watermarks tiles requested without a key ("API KEY REQUIRED"); a free key
      in CARTO_API_KEY removes it.
    - Esri Light Gray: similar look, no key needed. Fallback if CARTO is watermarked.
    - OpenStreetMap: blocks requests that send no Referer, which is every map opened
      straight from a file. Works when the map is served over localhost (SERVE_MAP).
    """
    carto_key = os.environ.get("CARTO_API_KEY", "").strip()
    carto_url = "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
    if carto_key:
        carto_url += f"?key={carto_key}"
    folium.TileLayer(
        tiles=carto_url, name="CARTO Positron" + ("" if carto_key else " (no key: watermarked)"),
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
             'contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
        subdomains="abcd", max_zoom=20,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/"
              "World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        name="Esri Light Gray",
        attr="Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors",
        max_zoom=16,
    ).add_to(m)
    folium.TileLayer(
        tiles="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        name="OpenStreetMap (needs localhost)",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        max_zoom=19,
    ).add_to(m)


def serve_map(path, port=8766):
    """Serve the output folder on localhost and open the map, so tile servers that
    require a Referer (OpenStreetMap) accept the requests. Ctrl+C to stop.
    (Port 8766: 8765 is the assistant's own web server.)"""
    import functools
    import http.server
    import socketserver
    import webbrowser
    folder, name = os.path.split(os.path.abspath(path))

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=folder)
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/{name}"
        print(f"\nServing map at {url}  (Ctrl+C to stop)")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def make_map(stops, clusters, routes, route_geoms, path):
    lat0 = float(np.mean([s["lat"] for s in stops]))
    lon0 = float(np.mean([s["lon"] for s in stops]))
    m = folium.Map(location=[lat0, lon0], zoom_start=10, tiles=None, prefer_canvas=True)
    add_basemaps(m)

    feeds = sorted({s["feed"] for s in stops} | {r["feed"] for r in routes.values()})
    feed_colour = {f: PALETTE[i % len(PALETTE)] for i, f in enumerate(feeds)}

    # Cross-agency clusters as translucent circles underneath everything
    hubs = folium.FeatureGroup(name="Cross-agency clusters", show=True)
    for c in clusters:
        if not c["cross_agency"]:
            continue
        folium.Circle(
            location=[c["centroid"]["lat"], c["centroid"]["lon"]],
            radius=MAX_DIST_KM / 2 * 1000,
            color="#555", weight=1, fill=True, fill_opacity=0.08,
            tooltip=f"Cluster {c['cluster_id']}: {c['n_stops']} stops, "
                    f"{c['n_feeds']} feeds, {c['n_routes']} routes",
        ).add_to(hubs)
    hubs.add_to(m)

    # Routes as edges, one layer per feed. Rail drawn on top and thicker.
    def draw_order(f):
        return (1 if "rail" in f else 0, f)
    for f in sorted(feeds, key=draw_order):
        feed_routes = [r for r in routes.values()
                       if r["feed"] == f and r["route_key"] in route_geoms]
        if not feed_routes:
            continue
        fg = folium.FeatureGroup(name=f"Routes: {f} ({len(feed_routes)})")
        for r in feed_routes:
            heavy = r["route_type"] in (0, 1, 2)
            folium.GeoJson(
                {"type": "Feature", "geometry": route_geoms[r["route_key"]], "properties": {}},
                style_function=lambda _, c=(r["color"] or feed_colour[f]), w=(5 if heavy else 2):
                    {"color": c, "weight": w, "opacity": 0.75},
                tooltip=f"{route_label(r)} — connects to {r['n_connecting_routes']} routes "
                        f"({r['n_cross_agency_connections']} other-agency)",
            ).add_to(fg)
        fg.add_to(m)

    # Stops as dots, one layer per feed
    for f in feeds:
        feed_stops = [s for s in stops if s["feed"] == f]
        if not feed_stops:
            continue
        fg = folium.FeatureGroup(name=f"Stops: {f} ({len(feed_stops)})")
        rail = "rail" in f
        for s in feed_stops:
            names = ", ".join(sorted(routes[rk]["short_name"] or routes[rk]["route_id"] or ""
                                     for rk in s["routes"]))[:300]
            folium.CircleMarker(
                location=[s["lat"], s["lon"]],
                radius=6 if rail else 3,
                color=feed_colour[f], fill=True, fill_opacity=0.9, weight=1,
                popup=folium.Popup(
                    f"<b>{s['name']}</b><br>{f}<br>stop_id: {s['stop_id']}"
                    f"<br>cluster: {s['cluster_id']}<br>routes: {names or '—'}",
                    max_width=280),
            ).add_to(fg)
        fg.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    m.save(path)


def simplify_geom(geom):
    if not geom or not geom.get("coordinates"):
        return None
    try:
        g = shape(geom)
        if SIMPLIFY_DEG:
            g = g.simplify(SIMPLIFY_DEG, preserve_topology=False)
        out = mapping(g)
        return json.loads(json.dumps(out), parse_float=lambda x: round(float(x), 5))
    except Exception:
        return geom


# --------------------------------------------------------------------------- main
def main():
    HEADERS["apikey"] = _api_key()
    all_stops, alias, routes, route_geoms = [], {}, {}, {}
    route_stop_refs = defaultdict(set)       # route_key -> {(feed, stop_id) kept}

    for feed in FEEDS:
        print(f"Fetching {feed} ...")
        try:
            raw_stops = fetch_stops(feed)
        except Exception as e:
            print(f"  stops FAILED: {e}")
            continue
        kept, a = normalise_stops(feed, raw_stops)
        alias.update(a)
        all_stops.extend(kept)
        print(f"  stops:  {len(raw_stops)} raw, {len(kept)} kept")
        if not raw_stops:
            print("  (no stops returned - check the feed key on transit.land)")

        try:
            raw_routes = fetch_routes(feed)
        except Exception as e:
            print(f"  routes FAILED: {e}")
            continue
        missing = 0
        for r in raw_routes:
            rec = normalise_route(feed, r)
            rk = rec["route_key"]
            routes[rk] = rec
            g = simplify_geom(r.get("geometry"))
            if g:
                route_geoms[rk] = g
            for rs in r.get("route_stops") or []:
                st = rs.get("stop") or {}
                target = alias.get(("id", st.get("id"))) or alias.get((feed, st.get("stop_id")))
                if target:
                    route_stop_refs[rk].add(target)
                else:
                    missing += 1
        print(f"  routes: {len(raw_routes)} "
              f"({sum(1 for r in raw_routes if route_key(feed, r) in route_geoms)} with shapes)"
              + (f", {missing} route-stop refs not matched" if missing else ""))

    if not all_stops:
        raise SystemExit("No stops fetched.")

    # attach routes to stops
    stop_index = {(s["feed"], s["stop_id"]): s for s in all_stops}
    for s in all_stops:
        s["routes"] = []
    for rk, refs in route_stop_refs.items():
        for ref in refs:
            if ref in stop_index:
                stop_index[ref]["routes"].append(rk)
    for s in all_stops:
        s["routes"].sort()
    for rk, r in routes.items():
        r["n_stops"] = len(route_stop_refs.get(rk, ()))

    labels = cluster_stops(all_stops)
    clusters = build_cluster_records(all_stops, labels, routes)
    route_info, pairs = find_route_connections(clusters, routes)
    for rk in routes:                         # map tooltips use connection counts
        routes[rk]["n_connecting_routes"] = route_info[rk]["n_connecting_routes"]
        routes[rk]["n_cross_agency_connections"] = route_info[rk]["n_cross_agency_connections"]

    with open(os.path.join(OUT_DIR, "stops_all.json"), "w") as f:
        json.dump(all_stops, f, indent=2)
    with open(os.path.join(OUT_DIR, "stop_clusters.json"), "w") as f:
        json.dump({
            "max_pairwise_distance_km": MAX_DIST_KM,
            "method": "greedy radius cover (radius = max_dist/2)",
            "n_stops": len(all_stops),
            "n_routes": len(routes),
            "n_clusters": len(clusters),
            "n_cross_agency_clusters": sum(c["cross_agency"] for c in clusters),
            "clusters": clusters,
        }, f, indent=2)
    with open(os.path.join(OUT_DIR, "route_connections.json"), "w") as f:
        json.dump({
            "rule": "two routes connect if they serve stops in the same cluster",
            "n_routes": len(route_info),
            "n_connecting_pairs": len(pairs),
            "n_cross_agency_pairs": sum(p["cross_agency"] for p in pairs),
            "routes": sorted(route_info.values(),
                             key=lambda r: -r["n_cross_agency_connections"]),
            "pairs": pairs,
        }, f, indent=2)
    make_map(all_stops, clusters, routes, route_geoms,
             os.path.join(OUT_DIR, "stop_clusters_map.html"))

    print(f"\n{len(all_stops)} stops -> {len(clusters)} clusters "
          f"({sum(c['cross_agency'] for c in clusters)} cross-agency)")
    print(f"{len(routes)} routes, {len(pairs)} connecting pairs "
          f"({sum(p['cross_agency'] for p in pairs)} cross-agency)")
    unserved = sum(1 for s in all_stops if not s["routes"])
    if unserved:
        print(f"note: {unserved} stops have no route attached")
    print("\nTop clusters:")
    for c in clusters[:10]:
        names = sorted({s['name'] for s in c['stops'] if 'rail' in s['feed']})[:3]
        print(f"  #{c['cluster_id']:>4}  {c['n_feeds']} feeds  {c['n_routes']:>3} routes  "
              f"{c['n_stops']:>4} stops  {names}")
    print("\nRoutes with the most other-agency connections:")
    for r in sorted(route_info.values(), key=lambda r: -r["n_cross_agency_connections"])[:10]:
        print(f"  {r['label']:<45} {r['n_cross_agency_connections']:>3} other-agency / "
              f"{r['n_connecting_routes']:>3} total")

    if SERVE_MAP:
        serve_map(os.path.join(OUT_DIR, "stop_clusters_map.html"))


if __name__ == "__main__":
    main()
