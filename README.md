# DMV Regional Transportation Assistant (`transit-mcp`)

A plain-language assistant that helps people — especially first-time riders — get
around the Washington DC / Maryland / Virginia region by public transit. It answers
from current, cited data pulled from **WMATA** (Metrorail + Metrobus) and the
connecting systems (**VRE, Virginia Breeze, Fairfax Connector, ART, DASH, CUE,
Loudoun, OmniRide**), and focuses on what trips actually hinge on: which systems
connect where, **on which days and at what times** a connection works, and **how to
pay** each leg.

> **Unofficial.** This is an independent project. It is **not affiliated with,
> endorsed by, sponsored by, or operated by** WMATA or any other transit agency.
> Agency names identify their services only. For official information, contact the
> agency directly.

Answers are grounded in the stored data: the assistant must call its tools before it
answers, is instructed never to fill gaps from the model's general knowledge, and says
"I don't have that information" rather than guess. (The Claude API no longer accepts a
`temperature` setting, so accuracy is enforced through the data, the tools and the
prompt instead — see `chat/agent.py`.)

## Quick start (Windows)

1. Install [Python 3.11+](https://www.python.org/downloads/) (tick *Add python.exe to PATH*).
2. **Double-click `start.bat`.**
   - First time: it sets everything up, creates a settings file and opens it in
     Notepad. Paste in your `ANTHROPIC_API_KEY`
     ([console.anthropic.com](https://console.anthropic.com/)) and `WMATA_API_KEY`
     (free at [developer.wmata.com](https://developer.wmata.com/)), save, and
     double-click `start.bat` again. It then downloads transit data (5–10 minutes).
   - After that: the assistant opens in your web browser within seconds.
3. Keep the black window open while you use it; close it to stop.

| File | What it does |
| --- | --- |
| `start.bat` | Set up if needed, then start the assistant and open the browser |
| `refresh-data.bat` | Download the latest alerts, schedules, fares and rider info (run weekly) |
| `check-setup.bat` | Plain-English check of keys, data, agencies and the trip planner |

Two agencies (VRE, CUE) block automated downloads — see
[feeds/README.md](feeds/README.md) to add their schedules by hand.

## Deploying (public website)

The same app runs as a public site — **https://dmvtransit.com** — on one small AWS
Lightsail server with Docker: `docker compose up -d --build` starts the website,
automatic HTTPS (Caddy) and a scheduler that keeps data fresh. Public mode adds
per-visitor rate limits, a site-wide daily cap and friendly error messages.
Step-by-step guide: **[deploy/README.md](deploy/README.md)**.

## Architecture

Three layers, each swappable on its own (details in
[docs/EXTENDING.md](docs/EXTENDING.md)):

```
 agencies/*.toml ──► ingest source plugins ──► SQLite (FTS5) + data/gtfs/*.zip
 one file per         gtfs · web_pages ·              │
 agency               wmata_api · (yours)             ▼
                                         retrieval + scheduling engine
                                         heuristic | python | process | http
                                                      │  TripRequest / TripPlan
                                                      ▼
                                   MCP server (14 tools) ──► web chat (Claude)
                                                         └─► any MCP client
```

| Layer | Extension point | Where |
| --- | --- | --- |
| **Agencies** | Drop a TOML file — no code | `agencies/`, built-ins in `src/transit_mcp/agencies/definitions/` |
| **Data sources** | `@register_source` plugin or entry point | `src/transit_mcp/ingest/sources/` |
| **Trip planning** | Your engine in Python, Node (stdio) or HTTP behind a versioned JSON contract | `src/transit_mcp/planning/`, templates in `engines/` |

Every stored record keeps its source URL, fetch time and content hash, so answers are
cited and stale data is flagged.

## Command line

All commands run from the project folder (`.venv\Scripts\transit-mcp.exe …`, or just
`transit-mcp …` with the virtual environment activated).

```bash
transit-mcp launch                         # what start.bat runs
transit-mcp doctor                         # what check-setup.bat runs
transit-mcp ingest all                     # what refresh-data.bat runs
transit-mcp ingest gtfs --only dash,vre    # one scope (api|gtfs|web), some agencies
transit-mcp agencies                       # every system with role + fare headline
transit-mcp agencies --config              # agency files, their sources, problems
transit-mcp status                         # row counts + freshness (JSON)
transit-mcp search "how do I pay with my phone"
transit-mcp chat --open                    # web chat only
transit-mcp serve                          # MCP server over stdio
transit-mcp planner request "Reston" "Pentagon"   # the JSON an engine receives
transit-mcp planner test "Reston" "Pentagon" --engine process --command "node engines/example-js/engine.js"
transit-mcp planner schema                 # write docs/planner-schema/*.json
```

Re-running `ingest` is safe and cheap: unchanged pages and feeds are detected by
content hash. One failing source never stops the rest of a run.

## Agencies loaded

| slug | agency | mode | data |
| --- | --- | --- | --- |
| `wmata` | Metro — Metrorail & Metrobus | rail + bus | WMATA API + wmata.com |
| `vre` | Virginia Railway Express | commuter rail | GTFS (manual download) |
| `fairfax-connector` | Fairfax Connector | bus | GTFS + fare page |
| `art` | Arlington Transit | bus | GTFS + fare pages |
| `dash` | DASH (Alexandria) — fare-free | bus | GTFS + rider page |
| `cue` | CUE (City of Fairfax) | bus | GTFS (manual download) |
| `loudoun` | Loudoun County Transit | bus | GTFS + fare pages |
| `omniride` | OmniRide (PRTC) | bus | GTFS + fare pages |
| `virginia-breeze` | Virginia Breeze | intercity coach | GTFS + booking page |

To add one, copy `agencies/_template.toml` — see [agencies/README.md](agencies/README.md).

## MCP tools

| Tool | Purpose |
| --- | --- |
| `search_transit_info(query, category?, limit?)` | Full-text search across all rider guidance, with citations |
| `get_service_alerts(line?, station?, mode?)` | Current Metro rail/bus disruptions |
| `get_elevator_escalator_status(station?)` | Elevator / escalator outages |
| `get_station_info(station)` | Lines, address, parking, entrances |
| `get_fare(origin, destination, rider_type?)` | Metrorail station-to-station fare |
| `list_topics()` / `get_guide(topic)` | Content overview / full rider guides |
| `list_agencies()` | Every system: role, strengths, service area, fare headline |
| `find_stops_near(place, radius_m?, agency?)` | Stops/stations near a place, across agencies |
| `get_route(agency, query)` | A route's days, hours, frequency, endpoints, fare |
| `transfers_at(place, day?, time?)` | Other systems within walking distance + whether the connection works then |
| `how_to_pay(agency)` | Payment methods, where to buy, app, SmarTrip/contactless, transfers |
| `plan_connection(origin, destination, when?)` | Systems per leg, transfer hub, days it works, fares — via the configured engine |
| `estimate_trip_cost(origin, destination, when?, rider_category?)` | Per-leg fares and total |

Resources: `transit://region/agencies`, `transit://wmata/freshness`,
`transit://wmata/guide/{topic}`.

### Using the MCP server from another client

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "dmv-transit": {
      "command": "C:\\path\\to\\transit_mcp\\.venv\\Scripts\\transit-mcp.exe",
      "args": ["serve"],
      "env": { "TRANSIT_MCP_DB": "C:\\path\\to\\transit_mcp\\transit.db" }
    }
  }
}
```

## Development

```bash
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

```bash
.venv\Scripts\python.exe -m pytest
```

Tests run offline against fixtures in `tests/fixtures/` (the Node engine test runs if
`node` is installed). Settings are documented in [.env.example](.env.example).

## Legal

See [NOTICE.md](NOTICE.md). WMATA API data is used under its Developer License
Agreement; GTFS feeds are published by each agency for redistribution; scraping is
limited to public, robots-allowed pages, rate-limited, and stored with attribution.
Not affiliated with WMATA or any agency listed.
