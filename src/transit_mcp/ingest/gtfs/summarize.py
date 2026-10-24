"""Derive per-route and per-stop service pictures + synthesized FTS documents.

The per-stop picture (``stop_service_summary``) is what the connection advisor uses
to answer "is a transfer here feasible on a Sunday evening?" — it records which days
of the week a stop has service and the first/last departure on weekdays, Saturdays,
and Sundays.
"""

from __future__ import annotations

import json
from datetime import datetime

from transit_mcp.db.database import Database
from transit_mcp.ingest.chunker import chunk_markdown
from transit_mcp.models import Document

_DAY_COLS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DOW_FROM_DATE = "%Y%m%d"


def _norm_time(t: str | None) -> str | None:
    """Zero-pad a GTFS HH:MM:SS so lexical compare == chronological (>24h allowed)."""
    if not t:
        return None
    parts = t.strip().split(":")
    if len(parts) != 3:
        return t
    return f"{int(parts[0]):02d}:{parts[1]}:{parts[2]}"


def _service_day_masks(db: Database, feed_id: str) -> dict[str, list[int]]:
    """service_id -> [mon..sun] 0/1, from calendar (+ calendar_dates fallback)."""
    masks: dict[str, list[int]] = {}
    for r in db.conn.execute(
        f"SELECT service_id, {', '.join(_DAY_COLS)} FROM gtfs_calendar WHERE feed_id=?",
        (feed_id,),
    ):
        masks[r["service_id"]] = [int(r[c] or 0) for c in _DAY_COLS]

    # Services with no weekly pattern (calendar_dates-only, or all-zero calendar
    # rows as in WMATA's feeds): infer weekdays from the added dates.
    weekly = {sid for sid, m in masks.items() if any(m)}
    rows = db.conn.execute(
        "SELECT service_id, date FROM gtfs_calendar_dates "
        "WHERE feed_id=? AND exception_type=1",
        (feed_id,),
    ).fetchall()
    for r in rows:
        if r["service_id"] in weekly:
            continue
        try:
            dow = datetime.strptime(r["date"], _DOW_FROM_DATE).weekday()  # Mon=0
        except ValueError:
            continue
        masks.setdefault(r["service_id"], [0] * 7)
        masks[r["service_id"]][dow] = 1
    return masks


def _days_label(mask: list[int]) -> str:
    wd = all(mask[:5])
    any_wd = any(mask[:5])
    sat, sun = mask[5], mask[6]
    if wd and sat and sun:
        return "Every day"
    if wd and sat and not sun:
        return "Mon–Sat"
    if wd and not sat and not sun:
        return "Mon–Fri"
    if not any_wd and sat and sun:
        return "Weekends"
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    picked = [n for n, d in zip(names, mask) if d]
    return ", ".join(picked) if picked else "No regular service"


def summarize_feed(db: Database, feed_id: str) -> dict:
    """Per-stop and per-route service windows for one feed.

    Streams ``stop_times`` and keeps only running aggregates (min/max times, counts),
    so memory stays flat even for very large feeds such as Metrobus (4M+ rows).
    """
    masks = _service_day_masks(db, feed_id)
    no_days = [0] * 7

    trip_meta = {
        t["trip_id"]: (t["route_id"], t["service_id"])
        for t in db.conn.execute(
            "SELECT trip_id, route_id, service_id FROM gtfs_trips WHERE feed_id=?",
            (feed_id,),
        )
    }
    stop_names = dict(
        db.conn.execute(
            "SELECT stop_id, name FROM gtfs_stops WHERE feed_id=?", (feed_id,)
        ).fetchall()
    )
    stop_coords = {
        r["stop_id"]: (r["lat"], r["lon"])
        for r in db.conn.execute(
            "SELECT stop_id, lat, lon FROM gtfs_stops WHERE feed_id=?", (feed_id,)
        )
    }

    def _window(agg: dict, key: str, dep: str) -> None:
        lo, hi = agg.get(key, (None, None))
        agg[key] = (dep if lo is None or dep < lo else lo, dep if hi is None or dep > hi else hi)

    stop_agg: dict[str, dict] = {}
    route_agg: dict[str, dict] = {}
    trip_stopcount: dict[str, int] = {}
    wk_count: dict[tuple[str, str], int] = {}  # (route, stop) -> weekday departures

    for st in db.conn.execute(
        "SELECT trip_id, stop_id, departure_time, arrival_time "
        "FROM gtfs_stop_times WHERE feed_id=?",
        (feed_id,),
    ):
        trip = st["trip_id"]
        trip_stopcount[trip] = trip_stopcount.get(trip, 0) + 1
        rid, sid = trip_meta.get(trip, (None, None))
        mask = masks.get(sid, no_days)
        dep = _norm_time(st["departure_time"] or st["arrival_time"])

        sa = stop_agg.get(st["stop_id"])
        if sa is None:
            sa = stop_agg[st["stop_id"]] = {"mask": [0] * 7, "routes": set(),
                                            "evening": False}
        sa["mask"] = [a | b for a, b in zip(sa["mask"], mask)]
        if rid:
            sa["routes"].add(rid)
        if dep:
            if any(mask[:5]):
                _window(sa, "wk", dep)
            if mask[5]:
                _window(sa, "sat", dep)
            if mask[6]:
                _window(sa, "sun", dep)
            if dep >= "20:00:00" and any(mask):
                sa["evening"] = True

        if rid:
            ra = route_agg.get(rid)
            if ra is None:
                ra = route_agg[rid] = {"mask": [0] * 7, "trips": set()}
            ra["mask"] = [a | b for a, b in zip(ra["mask"], mask)]
            ra["trips"].add(trip)
            if dep:
                _window(ra, "deps", dep)
                if any(mask[:5]):
                    wk_count[(rid, st["stop_id"])] = wk_count.get((rid, st["stop_id"]), 0) + 1

    # Trips per week at each stop: distinct trips (a loop may visit a stop twice).
    stop_trips_per_week: dict[str, int] = {}
    for r in db.conn.execute(
        "SELECT st.stop_id, t.service_id, COUNT(DISTINCT st.trip_id) AS n "
        "FROM gtfs_stop_times st JOIN gtfs_trips t "
        "ON t.feed_id = st.feed_id AND t.trip_id = st.trip_id "
        "WHERE st.feed_id=? GROUP BY st.stop_id, t.service_id",
        (feed_id,),
    ):
        stop_trips_per_week[r["stop_id"]] = (stop_trips_per_week.get(r["stop_id"], 0)
                                             + r["n"] * sum(masks.get(r["service_id"],
                                                                      no_days)))

    busiest: dict[str, tuple[str, int]] = {}
    for (rid, stop_id), n in wk_count.items():
        if n > busiest.get(rid, ("", 0))[1]:
            busiest[rid] = (stop_id, n)

    now = datetime.now().isoformat(sep=" ", timespec="seconds")

    db.conn.execute("DELETE FROM stop_service_summary WHERE feed_id=?", (feed_id,))
    for stop_id, a in stop_agg.items():
        m = a["mask"]
        wk, sat, sun = (a.get(k, (None, None)) for k in ("wk", "sat", "sun"))
        lat, lon = stop_coords.get(stop_id, (None, None))
        db.conn.execute(
            """
            INSERT INTO stop_service_summary
                (feed_id, stop_id, stop_name, lat, lon,
                 runs_mon, runs_tue, runs_wed, runs_thu, runs_fri, runs_sat, runs_sun,
                 weekday_first, weekday_last, saturday_first, saturday_last,
                 sunday_first, sunday_last, trips_per_week, route_ids,
                 serves_weekend, evening_service, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feed_id, stop_id, stop_names.get(stop_id), lat, lon,
                m[0], m[1], m[2], m[3], m[4], m[5], m[6],
                wk[0], wk[1], sat[0], sat[1], sun[0], sun[1],
                stop_trips_per_week.get(stop_id, 0), json.dumps(sorted(a["routes"])),
                int(m[5] or m[6]),
                int(a["evening"]),
                now,
            ),
        )

    db.conn.execute("DELETE FROM route_service_summary WHERE feed_id=?", (feed_id,))
    for route_id, a in route_agg.items():
        m = a["mask"]
        headway = None
        if route_id in busiest:
            deps = sorted(
                d for d in (
                    _norm_time(r["departure_time"] or r["arrival_time"])
                    for r in db.conn.execute(
                        "SELECT st.departure_time, st.arrival_time, t.service_id "
                        "FROM gtfs_stop_times st JOIN gtfs_trips t "
                        "ON t.feed_id = st.feed_id AND t.trip_id = st.trip_id "
                        "WHERE st.feed_id=? AND st.stop_id=? AND t.route_id=?",
                        (feed_id, busiest[route_id][0], route_id),
                    )
                    if any(masks.get(r["service_id"], no_days)[:5])
                ) if d
            )
            headway = _median_headway_min(deps)
        rep_trip = max(  # longest trip; ties broken by trip_id so runs are repeatable
            a["trips"], key=lambda t: (trip_stopcount.get(t, 0), t), default=None
        )
        first_name = last_name = None
        num_stops = trip_stopcount.get(rep_trip, 0) if rep_trip else 0
        if rep_trip:
            seq = db.conn.execute(
                "SELECT stop_id FROM gtfs_stop_times WHERE feed_id=? AND trip_id=? "
                "ORDER BY stop_sequence",
                (feed_id, rep_trip),
            ).fetchall()
            if seq:
                first_name = stop_names.get(seq[0]["stop_id"])
                last_name = stop_names.get(seq[-1]["stop_id"])
        trips_per_week = sum(
            sum(masks.get(trip_meta.get(t, (None, None))[1], [0] * 7))
            for t in a["trips"]
        )
        db.conn.execute(
            """
            INSERT INTO route_service_summary
                (feed_id, route_id, service_days, runs_weekday, runs_saturday,
                 runs_sunday, first_departure, last_departure, trips_per_week,
                 typical_headway_min, num_stops, first_stop_name, last_stop_name,
                 computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feed_id, route_id, _days_label(m),
                int(any(m[:5])), int(m[5]), int(m[6]),
                a.get("deps", (None, None))[0],
                a.get("deps", (None, None))[1],
                trips_per_week, headway, num_stops, first_name, last_name, now,
            ),
        )

    db.commit()
    return {
        "feed_id": feed_id,
        "stops_summarized": len(stop_agg),
        "routes_summarized": len(route_agg),
    }


def _median_headway_min(sorted_times: list[str]) -> int | None:
    if len(sorted_times) < 2:
        return None

    def mins(t: str) -> int:
        h, m, s = t.split(":")
        return int(h) * 60 + int(m)

    gaps = [mins(b) - mins(a) for a, b in zip(sorted_times, sorted_times[1:])]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    gaps.sort()
    return int(gaps[len(gaps) // 2])


# ---------------------------------------------------------------------------
# Synthesized FTS documents
# ---------------------------------------------------------------------------
def synthesize_docs(db: Database, feed_id: str) -> int:
    prof = db.conn.execute(
        "SELECT * FROM agency_profiles WHERE feed_id=?", (feed_id,)
    ).fetchone()
    fare = db.conn.execute(
        "SELECT * FROM fare_vending WHERE feed_id=?", (feed_id,)
    ).fetchone()
    feed = db.conn.execute(
        "SELECT * FROM gtfs_feeds WHERE feed_id=?", (feed_id,)
    ).fetchone()
    name = (prof["display_name"] if prof else None) or (
        feed["agency_name"] if feed else feed_id
    )
    now = datetime.now()

    # -- agency profile doc --
    lines = [f"# {name}", ""]
    if prof:
        lines += [
            f"**Role:** {prof['role']}", "",
            f"**Strengths:** {prof['strengths']}", "",
            f"**Weaknesses / limits:** {prof['weaknesses']}", "",
            f"**Service area:** {prof['service_area']}", "",
        ]
        hubs = json.loads(prof["connects_to"] or "[]")
        if hubs:
            lines += ["**Connects to:**", *[f"- {h}" for h in hubs], ""]
    if fare:
        pm = json.loads(fare["payment_methods"] or "[]")
        wb = json.loads(fare["where_to_buy"] or "[]")
        lines += ["## Fares and how to pay", ""]
        if fare["base_fare"] is not None:
            lines.append(
                f"Base fare: ${fare['base_fare']:.2f}"
                + (
                    f" (reduced ${fare['reduced_fare']:.2f})"
                    if fare["reduced_fare"] is not None
                    else ""
                )
            )
        lines += ["", "How to pay:", *[f"- {p}" for p in pm], ""]
        if wb:
            lines += ["Where to buy:", *[f"- {w}" for w in wb], ""]
        if fare["mobile_app_name"]:
            lines.append(f"Mobile app: {fare['mobile_app_name']}")
        if fare["transfer_policy"]:
            lines += ["", f"Transfers: {fare['transfer_policy']}"]
        if fare["notes"]:
            lines += ["", fare["notes"]]
        lines += ["", f"Fare page: {fare['fare_page_url']}"]
    if feed and feed["feed_start_date"]:
        lines += [
            "", f"_Schedule data valid {feed['feed_start_date']}–"
            f"{feed['feed_end_date'] or 'ongoing'}._",
        ]
    body = "\n".join(lines) + "\n"
    _store_doc(
        db, feed_id, url=f"gtfs://{feed_id}/agency", category="agency",
        title=f"{name} — role, strengths, and how to pay", body=body, now=now,
    )
    count = 1

    # -- per-route docs --
    routes = db.conn.execute(
        """
        SELECT r.route_id, r.short_name, r.long_name, r.description, r.route_url,
               s.service_days, s.first_departure, s.last_departure,
               s.typical_headway_min, s.num_stops, s.first_stop_name, s.last_stop_name,
               s.runs_weekday, s.runs_saturday, s.runs_sunday
          FROM gtfs_routes r
          LEFT JOIN route_service_summary s
            ON s.feed_id = r.feed_id AND s.route_id = r.route_id
         WHERE r.feed_id = ?
        """,
        (feed_id,),
    ).fetchall()
    for r in routes:
        label = " ".join(x for x in (r["short_name"], r["long_name"]) if x) or r["route_id"]
        span = (
            f"{r['first_departure']}–{r['last_departure']}"
            if r["first_departure"]
            else "see schedule"
        )
        hw = (
            f"about every {r['typical_headway_min']} min"
            if r["typical_headway_min"]
            else "frequency varies"
        )
        rbody = "\n".join([
            f"# {name}: Route {label}",
            "",
            r["description"] or f"{name} route {label}.",
            "",
            f"- Runs: {r['service_days'] or 'see schedule'}",
            f"- Service span: {span}",
            f"- Typical weekday frequency: {hw}",
            f"- Stops: {r['num_stops'] or '?'} "
            f"({r['first_stop_name'] or '?'} ↔ {r['last_stop_name'] or '?'})",
            f"- Weekend: {'Saturday ' if r['runs_saturday'] else ''}"
            f"{'Sunday' if r['runs_sunday'] else ''}".strip() or "weekday only",
        ]) + "\n"
        if r["route_url"]:
            rbody += f"\nMore: {r['route_url']}\n"
        _store_doc(
            db, feed_id, url=f"gtfs://{feed_id}/route/{r['route_id']}",
            category="route", title=f"{name}: Route {label}", body=rbody, now=now,
        )
        count += 1

    db.commit()
    return count


def _store_doc(db: Database, feed_id: str, *, url: str, category: str, title: str,
               body: str, now: datetime) -> None:
    import hashlib

    raw_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    source_id, _ = db.upsert_source(
        url=url, source_type="api", category=category, feed_id=feed_id,
        http_status=200, content_hash=raw_hash,
    )
    doc = Document(
        url=url, category=category, feed_id=feed_id, title=title,
        summary=body.split("\n\n", 1)[0][:280].replace("\n", " ").strip() or None,
        body_markdown=body, raw_hash=raw_hash, retrieved_at=now,
    )
    db.replace_document(
        source_id=source_id, document=doc,
        chunks=chunk_markdown(body, doc_title=title),
    )
