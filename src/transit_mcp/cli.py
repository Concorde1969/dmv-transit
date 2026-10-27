"""Command-line entry point for transit-mcp."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from transit_mcp.config import get_settings
from transit_mcp.db.database import Database, init_db


def _split(value: str | None) -> list[str] | None:
    return [s.strip() for s in value.split(",") if s.strip()] if value else None


def _ingest_summary(result: dict) -> str:
    """One readable line per agency for people who don't want JSON."""
    lines = []
    for slug, sources in (result.get("agencies") or {}).items():
        parts = []
        for stype, stats in sources.items():
            if not isinstance(stats, dict):
                continue
            if stats.get("status") == "error":
                parts.append(f"{stype}: FAILED ({stats.get('error')})")
            elif stype.startswith("gtfs_rt_alerts"):
                parts.append(f"{stats.get('alerts', 0)} live alerts"
                             if "alerts" in stats else f"alerts: {stats.get('skipped')}")
            elif stype.startswith("gtfs"):
                via = stats.get("fetched_via")
                parts.append(f"schedules {stats.get('status', 'done')}"
                             + (f" (via {via})" if via else ""))
            elif stype.startswith("web_pages"):
                parts.append(f"{stats.get('fetched', 0)} web pages")
            elif stype.startswith("wmata_api"):
                parts.append(f"{stats.get('stations', 0)} stations, live alerts")
            else:
                parts.append(f"{stype} done")
        lines.append(f"  - {slug}: " + "; ".join(parts))
    links = (result.get("post") or {}).get("links")
    if links:
        lines.append(f"  - transfer points between systems: {links.get('links', 0)}")
    for n in result.get("notices") or []:
        lines.append(f"  note: {n}")
    for e in result.get("errors") or []:
        lines.append(f"  problem: {e}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def _cmd_init_db(args: argparse.Namespace) -> int:
    settings = get_settings()
    init_db(settings.db_path)
    print(f"Initialized schema at {settings.db_path}")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from transit_mcp.ingest.run import run_all, run_api, run_gtfs, run_web

    settings = get_settings()
    init_db(settings.db_path)  # ensure schema exists / migrated
    only = _split(args.only)
    load_stop_times = not args.no_stop_times

    if args.scope == "web":
        result = run_web(settings, max_pages=args.max_pages, force=args.force, only=only)
    elif args.scope == "api":
        result = run_api(settings, include_fares=args.fares, only=only)
    elif args.scope == "gtfs":
        result = run_gtfs(settings, only=only, load_stop_times=load_stop_times)
    else:
        result = run_all(
            settings, max_pages=args.max_pages, force=args.force,
            include_fares=args.fares, only=only, load_stop_times=load_stop_times,
        )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(_ingest_summary(result))
    return 1 if result.get("errors") and not result.get("agencies") else 0


def _cmd_agencies(args: argparse.Namespace) -> int:
    if args.config:
        from transit_mcp.agencies import discover

        agencies, errors = discover(get_settings(), include_disabled=True)
        for a in agencies:
            flag = "" if a.enabled else "  (disabled)"
            srcs = ", ".join(s.type + ("" if s.enabled else " [off]") for s in a.sources)
            print(f"{a.slug:20} {a.mode:15} {srcs:30} {a.origin}{flag}")
        for e in errors:
            print(f"ERROR {e}")
        return 1 if errors else 0

    from transit_mcp.retrieval.regional import list_agencies

    with Database(get_settings().db_path) as db:
        rows = list_agencies(db)
    for a in rows:
        fresh = a.get("feed_fetched_at") or "—"
        print(f"\n{a['display_name']}  [{a.get('mode') or a.get('feed_id')}]")
        if a.get("role"):
            print(f"  role:      {a['role']}")
        if a.get("fare_headline"):
            print(f"  fares:     {a['fare_headline']}")
        print(f"  status:    {a.get('status', 'n/a')}   data: {fresh}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from transit_mcp.retrieval.queries import freshness_report

    with Database(get_settings().db_path) as db:
        report = freshness_report(db)
    print(json.dumps(report, indent=2, default=str))
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    from transit_mcp.retrieval.search import search

    with Database(get_settings().db_path) as db:
        hits = search(db, args.query, category=args.category, limit=args.limit)
    if not hits:
        print("No results.")
        return 1
    for h in hits:
        print(f"\n## {h.title}  [{h.category}]  score={h.score:.3f}")
        print(f"   {h.url}   (retrieved {h.retrieved_at})")
        if h.heading_path:
            print(f"   section: {h.heading_path}")
        print()
        print("   " + h.snippet.replace("\n", "\n   ")[:900])
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from transit_mcp.server.app import main as serve_main

    serve_main()
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    from transit_mcp.chat.app import serve as chat_serve

    settings = get_settings()
    print(f"Assistant running at http://{settings.chat_host}:{settings.chat_port}")
    chat_serve(open_browser=args.open)
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from transit_mcp.doctor import FAIL, format_checks, run_checks

    settings = get_settings()
    init_db(settings.db_path)
    checks = run_checks(settings)
    print("Checking the DMV Regional Transportation Assistant...\n")
    print(format_checks(checks))
    failed = [c for c in checks if c.status == FAIL]
    print("\nAll good." if not failed else f"\n{len(failed)} problem(s) need fixing.")
    return 1 if failed else 0


def _cmd_launch(args: argparse.Namespace) -> int:
    """Everything start.bat needs: migrate, check, load data if empty, run, open."""
    from transit_mcp.chat.app import serve as chat_serve
    from transit_mcp.doctor import FAIL, format_checks, has_data, run_checks

    settings = get_settings()
    print("Starting the DMV Regional Transportation Assistant...\n")
    init_db(settings.db_path)

    checks = run_checks(settings, check_planner=False)
    blocking = [c for c in checks if c.status == FAIL]
    if blocking:
        print(format_checks(checks))
        print("\nThe assistant can't start until the problem(s) above are fixed.")
        return 1

    if not has_data(settings) and not args.skip_ingest:
        from transit_mcp.ingest.run import run_all

        print("First run: downloading transit information for every agency.")
        print("This usually takes 5 to 10 minutes. Please leave this window open.\n")
        result = run_all(settings)
        print(_ingest_summary(result))
        print()

    url = f"http://{settings.chat_host}:{settings.chat_port}"
    print(f"The assistant is ready at {url}")
    if not args.no_browser:
        print("Your web browser should open by itself in a moment.")
    print("Keep this window open while you use it. To stop, close this window "
          "or press Ctrl+C.\n")
    chat_serve(open_browser=not args.no_browser)
    return 0


def _cmd_scheduler(args: argparse.Namespace) -> int:
    import logging

    from transit_mcp.scheduler import Scheduler

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise
    sched = Scheduler(get_settings())
    if args.once:
        sched.start()
        sched.tick()
        return 0
    sched.run_forever()
    return 0


def _cmd_chatlog(args: argparse.Namespace) -> int:
    from transit_mcp.chatlog import ChatLog

    settings = get_settings()
    if not Path(settings.chatlog_db).is_file():
        print(f"No chat log yet at {settings.chatlog_db}.", file=sys.stderr)
        return 1
    chatlog = ChatLog.from_settings(settings)
    try:
        if args.chatlog_cmd == "export":
            export = chatlog.export_csv if args.format == "csv" else chatlog.export_jsonl
            # Binary write: identical bytes on Windows and Linux, UTF-8 regardless of console.
            sys.stdout.buffer.write(export(args.since, args.until).encode("utf-8"))
        elif args.chatlog_cmd == "stats":
            print(json.dumps(chatlog.stats(args.days), indent=2))
        elif args.chatlog_cmd == "delete":
            n = chatlog.delete(conversation=args.conversation, visitor=args.visitor)
            print(f"Deleted {n} question(s).")
        elif args.chatlog_cmd == "prune":
            n = chatlog.prune(settings.chatlog_retention_days)
            print(f"Removed {n} question(s) older than {settings.chatlog_retention_days} days.")
    finally:
        chatlog.close()
    return 0


def _cmd_reports(args: argparse.Namespace) -> int:
    from transit_mcp.reports import ReportStore

    settings = get_settings()
    store = ReportStore.from_settings(settings)
    if args.reports_cmd == "list":
        rows = store.list_all(args.days)
        if args.status:
            rows = [r for r in rows if r["status"] == args.status]
        sys.stdout.buffer.write(json.dumps(rows, indent=2).encode("utf-8"))
        print()
    elif args.reports_cmd in ("hide", "publish"):
        status = "hidden" if args.reports_cmd == "hide" else "published"
        ok = store.set_status(args.id, status)
        print(f"Report {args.id} {'is now ' + status if ok else 'not found'}.")
    elif args.reports_cmd == "delete":
        print(f"Report {args.id} {'deleted' if store.delete(args.id) else 'not found'}.")
    elif args.reports_cmd == "prune":
        n = store.prune(settings.reports_retention_days)
        print(f"Removed {n} report(s) older than {settings.reports_retention_days} days.")
    return 0


def _cmd_planner(args: argparse.Namespace) -> int:
    from transit_mcp.planning import TripPlan, TripRequest, get_planner
    from transit_mcp.planning.request import UnresolvedPlace, build_request

    settings = get_settings()
    overrides = {k: v for k, v in (
        ("planner_engine", args.engine if hasattr(args, "engine") else None),
        ("planner_command", getattr(args, "command_line", None)),
        ("planner_python", getattr(args, "python_target", None)),
        ("planner_url", getattr(args, "url", None)),
    ) if v}
    if overrides:
        settings = settings.model_copy(update=overrides)

    if args.planner_cmd == "schema":
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for name, model in (("trip-request", TripRequest), ("trip-plan", TripPlan)):
            path = out / f"{name}.schema.json"
            path.write_text(json.dumps(model.model_json_schema(), indent=2), "utf-8")
            print(f"wrote {path}")
        return 0

    with Database(settings.db_path) as db:
        try:
            request = build_request(db, args.origin, args.destination, day=args.day,
                                    settings=settings)
        except UnresolvedPlace as exc:
            print(f"Could not find: origin={exc.origin_label!r} "
                  f"destination={exc.destination_label!r}")
            return 1

    if args.planner_cmd == "request":
        print(json.dumps(request.model_dump(mode="json"), indent=2))
        return 0

    # planner test: no fallback, so engine problems surface
    try:
        planner = get_planner(settings, fallback=False)
        info = planner.describe()
        print(f"engine: {json.dumps(info)}")
        plan = planner.plan(request)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}")
        return 1
    print(f"plan from engine '{plan.engine}': {plan.origin} -> {plan.destination}")
    for i, opt in enumerate(plan.options, 1):
        print(f"  {i}. {opt.summary}  [feasible={opt.feasible}] {opt.feasibility or ''}")
        for leg in opt.legs:
            print(f"       - {leg.mode}: {leg.system or leg.agency or ''} "
                  f"{leg.board or ''} -> {leg.alight or ''}".rstrip())
    for w in plan.warnings:
        print(f"  warning: {w}")
    print("OK — the engine's reply matches the TripPlan contract.")
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="transit-mcp", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("launch", help="Set up if needed, then start the assistant (start.bat)") \
        .set_defaults(func=_cmd_launch, no_browser=False, skip_ingest=False)
    launch = sub.choices["launch"]
    launch.add_argument("--no-browser", action="store_true")
    launch.add_argument("--skip-ingest", action="store_true",
                        help="Don't download data on first run")

    sub.add_parser("doctor", help="Check settings, data, agencies and planner") \
        .set_defaults(func=_cmd_doctor)

    sub.add_parser("init-db", help="Create or upgrade the SQLite schema") \
        .set_defaults(func=_cmd_init_db)

    ing = sub.add_parser("ingest", help="Fetch data and populate the database")
    ing.add_argument("scope", choices=["api", "web", "gtfs", "all"], nargs="?", default="all")
    ing.add_argument("--only", default=None,
                     help="comma-separated agency slugs, e.g. dash,art,vre")
    ing.add_argument("--max-pages", type=int, default=None,
                     help="override each web_pages source's page cap")
    ing.add_argument("--force", action="store_true",
                     help="re-store documents even if unchanged")
    ing.add_argument("--no-fares", dest="fares", action="store_false",
                     help="skip the WMATA station-to-station fare matrix")
    ing.add_argument("--no-stop-times", action="store_true",
                     help="skip GTFS stop_times.txt (faster; loses departure data)")
    ing.add_argument("--json", action="store_true", help="print full JSON results")
    ing.set_defaults(func=_cmd_ingest, fares=True)

    sub.add_parser("status", help="Row counts and data freshness (JSON)") \
        .set_defaults(func=_cmd_status)

    ag = sub.add_parser("agencies", help="List transit systems (from the database)")
    ag.add_argument("--config", action="store_true",
                    help="list agency definition files and their sources instead")
    ag.set_defaults(func=_cmd_agencies)

    srch = sub.add_parser("search", help="Debug: run a full-text search")
    srch.add_argument("query")
    srch.add_argument("--category", default=None)
    srch.add_argument("--limit", type=int, default=5)
    srch.set_defaults(func=_cmd_search)

    sub.add_parser("serve", help="Run the MCP server over stdio") \
        .set_defaults(func=_cmd_serve)

    sch = sub.add_parser("scheduler", help="Keep data fresh on a server (runs forever)")
    sch.add_argument("--once", action="store_true",
                     help="first-run load if empty + one alerts refresh, then exit")
    sch.set_defaults(func=_cmd_scheduler)

    chat = sub.add_parser("chat", help="Run the local web chat UI")
    chat.add_argument("--open", action="store_true", help="open the browser when ready")
    chat.set_defaults(func=_cmd_chat)

    cl = sub.add_parser("chatlog", help="Review the saved chats (export, stats, delete)")
    clsub = cl.add_subparsers(dest="chatlog_cmd", required=True)
    ex = clsub.add_parser("export", help="Write saved chats to stdout (CSV or JSON lines)")
    ex.add_argument("--format", choices=["csv", "jsonl"], default="csv")
    ex.add_argument("--since", help="YYYY-MM-DD (inclusive)")
    ex.add_argument("--until", help="YYYY-MM-DD (exclusive)")
    st = clsub.add_parser("stats", help="Totals for the last N days (JSON)")
    st.add_argument("--days", type=int, default=7)
    de = clsub.add_parser("delete", help="Delete one conversation or one visitor's chats")
    who = de.add_mutually_exclusive_group(required=True)
    who.add_argument("--conversation")
    who.add_argument("--visitor")
    clsub.add_parser("prune", help="Delete chats older than CHATLOG_RETENTION_DAYS")
    cl.set_defaults(func=_cmd_chatlog)

    rp = sub.add_parser("reports", help="Review rider reports (list, hide, publish, delete)")
    rpsub = rp.add_subparsers(dest="reports_cmd", required=True)
    rl = rpsub.add_parser("list", help="Reports from the last N days (JSON)")
    rl.add_argument("--days", type=int, default=7)
    rl.add_argument("--status", choices=["pending", "published", "hidden"])
    for name, helptext in (("hide", "Stop showing a report"),
                           ("publish", "Show a report that was held back"),
                           ("delete", "Delete a report")):
        rpsub.add_parser(name, help=helptext).add_argument("id", type=int)
    rpsub.add_parser("prune", help="Delete reports older than REPORTS_RETENTION_DAYS")
    rp.set_defaults(func=_cmd_reports)

    pl = sub.add_parser("planner", help="Work with trip-planning engines")
    plsub = pl.add_subparsers(dest="planner_cmd", required=True)
    sch = plsub.add_parser("schema", help="Write the TripRequest/TripPlan JSON Schemas")
    sch.add_argument("--out", default="docs/planner-schema")
    for name, helptext in (
        ("request", "Print the TripRequest an engine would receive"),
        ("test", "Send a real request to the configured engine and validate the reply"),
    ):
        sp = plsub.add_parser(name, help=helptext)
        sp.add_argument("origin")
        sp.add_argument("destination")
        sp.add_argument("--day", default=None, help="e.g. saturday, weekday")
        if name == "test":
            sp.add_argument("--engine", choices=["heuristic", "python", "process", "http"])
            sp.add_argument("--command", dest="command_line",
                            help="process engine command line")
            sp.add_argument("--python", dest="python_target",
                            help="python engine module:factory or file.py:factory")
            sp.add_argument("--url", help="http engine base URL")
    pl.set_defaults(func=_cmd_planner)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
