# DMV Regional Transportation Assistant (`transit-mcp`)

A plain-language assistant that helps people — especially first-time riders — get
around the Washington DC / Maryland / Virginia region by public transit. It answers
from current, cited data pulled from **WMATA** (Metrorail + Metrobus), the Virginia
systems (**VRE, Fairfax Connector, ART, DASH, CUE, Loudoun, OmniRide, Virginia
Breeze**), Maryland (**MTA bus, commuter bus, Metro SubwayLink, Light RailLink, MARC,
Annapolis Transit, Harbor Connector, Potomac Valley**) and intercity services
(**Amtrak, FlixBus** and other intercity buses), and focuses on what trips actually
hinge on: which systems connect where, **on which days and at what times** a
connection works, and **how to pay** each leg.

The home page is a **leave-now trip planner on a map**: type where you're going (or use
your location), and it shows every good way to get there in the next hour — direct
trips first, one change only when it saves time, never a wait of more than 10 minutes
at a change — with live departure times where the agency publishes them. It uses each
agency's closest stops (up to about a mile away) and shows how much walking each trip
involves. The assistant chat sits in a drawer beside the map.

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
     double-click `start.bat` again. It then downloads transit data (15–20 minutes
     for all 21 feeds).
   - For the map planner, also add `TRANSITLAND_API_KEY`
     ([transit.land](https://www.transit.land/), Explorer plan) and `GEOCODE_API_KEY`
     ([geocode.maps.co](https://geocode.maps.co/)), and download the regional map once
     (about 600 MB; without it the map shows a plain background):
     `powershell -ExecutionPolicy Bypass -File deploy\map\fetch-basemap.ps1`
   - After that: the assistant opens in your web browser within seconds.
3. Keep the black window open while you use it; close it to stop.

| File | What it does |
| --- | --- |
| `start.bat` | Set up if needed, then start the assistant and open the browser |
| `refresh-data.bat` | Download the latest alerts, schedules, fares and rider info (run weekly) |
| `check-setup.bat` | Plain-English check of keys, data, agencies and the trip planner |

If a feed can't be downloaded, you can add it by hand — see
[feeds/README.md](feeds/README.md).

## Deploying (public website)

The same app runs as a public site — **https://dmvtransit.com** — on one small AWS
Lightsail server in Ohio (us-east-2) with Docker, created and managed by the scripts in
`deploy/lightsail/`: `docker compose up -d --build` starts the website,
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
                                   MCP server (15 tools) ──► web chat (Claude)
                                                         └─► any MCP client
```

| Layer | Extension point | Where |
| --- | --- | --- |
| **Agencies** | Drop a TOML file — no code | `agencies/`, built-ins in `src/transit_mcp/agencies/definitions/` |
| **Data sources** | `@register_source` plugin or entry point | `src/transit_mcp/ingest/sources/` |
| **Trip planning** | Your engine in Python, Node (stdio) or HTTP behind a versioned JSON contract | `src/transit_mcp/planning/`, templates in `engines/` |

Every stored record keeps its source URL, fetch time and content hash, so answers are
cited and stale data is flagged.

### Where each kind of answer comes from

| Answer | Source |
| --- | --- |
| Routes, stops, schedules, service days and hours (Metrorail and Metrobus included) | Each agency's **GTFS** feed, found through **[Transitland](https://www.transit.land/)** |
| Fares, how to pay, how to ride | The agency's own **fare / rider web pages**, plus WMATA's station-to-station fare data (checked against wmata.com/pay.html) |
| Disruptions | WMATA real-time incidents, agencies' GTFS-realtime alerts, and their service-alert / service-change web pages — always shown *alongside* the GTFS schedule, never instead of it |
| Live departure times (map planner) | The agency's own **GTFS-realtime TripUpdates** feed at the URL Transitland lists (WMATA rail and bus, VRE, Fairfax Connector, MARC, ART); Transitland's departures API for the rest. Otherwise scheduled times, labelled "Scheduled" |
| Addresses → map points | [geocode.maps.co](https://geocode.maps.co/), called from the server (the key never reaches browsers) |
| The map | [OpenStreetMap](https://www.openstreetmap.org/copyright) data in a self-hosted [Protomaps](https://protomaps.com/) extract, drawn with MapLibre GL |

Web pages are never used for routes or schedules. Each feed is downloaded from the
official URL listed in Transitland's public registry (no API quota used); Transitland's
own hosted copy is the fallback, at one metered API call each (`transit-mcp doctor`
shows which source each feed used). Every Transitland API call is counted against
`TRANSITLAND_MONTHLY_BUDGET` (4,500 of the Explorer plan's 5,000), after which the
planner shows scheduled times.

Nearby stops of every agency are grouped into **clusters** — every pair of stops in a
cluster is less than 0.3 km apart — and the planner only suggests a change between two
stops of the same cluster, so every transfer is a short walk.

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
| `wmata` | Metrorail (WMATA) | rail | GTFS via Transitland + WMATA API (stations, incidents, fares) + wmata.com (fares, alerts, guides) |
| `metrobus` | Metrobus (WMATA) | bus | GTFS via Transitland (+ WMATA bus incidents) |
| `vre` | Virginia Railway Express | commuter rail | GTFS via Transitland |
| `fairfax-connector` | Fairfax Connector | bus | GTFS via Transitland + GTFS-realtime alerts + fare page |
| `art` | Arlington Transit | bus | GTFS via Transitland + fare and alert pages |
| `dash` | DASH (Alexandria) — fare-free | bus | GTFS via Transitland + rider page |
| `cue` | CUE (City of Fairfax) | bus | GTFS via Transitland |
| `loudoun` | Loudoun County Transit | bus | GTFS via Transitland + fare and news pages |
| `omniride` | OmniRide (PRTC) | bus | GTFS via Transitland + fare and service-update pages |
| `virginia-breeze` | Virginia Breeze | intercity coach | GTFS via Transitland + booking page |
| `mta-md-bus` | MTA Maryland Local Bus | bus | GTFS via Transitland |
| `mta-md-commuter-bus` | MTA Maryland Commuter Bus | commuter bus | GTFS via Transitland |
| `mta-md-metro` | MTA Maryland Metro SubwayLink | rail | GTFS via Transitland |
| `mta-md-light-rail` | MTA Maryland Light RailLink | rail | GTFS via Transitland |
| `marc` | MARC Train | commuter rail | GTFS via Transitland |
| `annapolis-transit` | Annapolis Transit | bus | GTFS via Transitland |
| `harbor-connector` | Harbor Connector (Baltimore) | ferry | GTFS via Transitland |
| `potomac-valley` | Potomac Valley Transit Authority | bus | GTFS via Transitland |
| `intercity-bus` | Intercity buses (combined feed) | intercity bus | GTFS via Transitland |
| `flixbus` | FlixBus | intercity bus | GTFS via Transitland |
| `amtrak` | Amtrak | intercity rail | GTFS via Transitland |

The Maryland and intercity agencies have no hand-written fare or rider pages yet: the
assistant points riders to each agency's own fare page from its GTFS feed.

To add one, copy `agencies/_template.toml` — see [agencies/README.md](agencies/README.md).

## MCP tools

| Tool | Purpose |
| --- | --- |
| `search_transit_info(query, category?, limit?)` | Search the agencies' fare / how-to / alert pages (never routes or schedules) |
| `get_service_alerts(line?, station?, mode?, agency?)` | Live disruptions: WMATA incidents + agencies' GTFS-realtime alerts |
| `get_elevator_escalator_status(station?)` | Elevator / escalator outages |
| `get_station_info(station)` | Lines, address, parking, entrances |
| `get_fare(origin, destination, rider_type?)` | Metrorail station-to-station fare |
| `get_metro_route(origin, destination)` | Metrorail lines, changes of train, direction, first/last trains, disruptions |
| `list_topics()` / `get_guide(topic)` | Content overview / full rider guides |
| `list_agencies()` | Every system: role, strengths, service area, fare headline |
| `find_stops_near(place, radius_m?, agency?)` | Stops/stations near a place, across agencies |
| `get_route(agency, query)` | A route's days, hours, frequency, endpoints, fare |
| `transfers_at(place, day?, time?)` | Other systems within walking distance + whether the connection works then |
| `how_to_pay(agency)` | Payment methods, where to buy, app, SmarTrip/contactless, transfers |
| `plan_connection(origin, destination, when?)` | Systems per leg, transfer hub, days it works, fares — via the configured engine |
| `plan_trip_now(origin, destination)` | The map planner's leave-now options for the next hour, with live times and walking minutes |
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
Agreement; GTFS feeds are published by each agency (located through Transitland); scraping is
limited to public, robots-allowed pages, rate-limited, and stored with attribution.
Not affiliated with WMATA or any agency listed.
