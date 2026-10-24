# Extending the DMV Regional Transportation Assistant

The app is built from three swappable layers. Each has one extension point, and none
of them require touching the others.

```
 agencies/*.toml ──►  ingest source plugins  ──►  SQLite + data/gtfs/*.zip
 (what to load)       (how to load it)               │
                                                      ▼
                       scheduling engine  ◄──  TripRequest / TripPlan contract
                       (heuristic | python | process | http)
                                                      │
                                                      ▼
                             MCP tools  ──►  chat UI / any MCP client
```

| I want to… | Do this | Code changes? |
| --- | --- | --- |
| Add a transit agency | Drop a TOML file in `agencies/` | None |
| Change an agency's fares, pages or feed URL | Edit its TOML file | None |
| Load a new *kind* of data (e.g. an agency's own real-time API) | Write a source plugin | One small Python class |
| Plug in my own scheduling engine | Point `PLANNER_*` settings at it | None in this repo |

---

## 1. Add an agency

```bash
copy agencies\_template.toml agencies\my-agency.toml
```

Fill in the file (every field is explained inline), then check and load it:

```bash
.venv\Scripts\transit-mcp.exe agencies --config
```

```bash
.venv\Scripts\transit-mcp.exe ingest --only my-agency
```

That's it — the agency now appears in `list_agencies`, search, transfer points, the
trip planner, `refresh-data.bat`, and the chat.

**How the file is used**

| Section | Feeds | Used by |
| --- | --- | --- |
| `[profile]` | `agency_profiles` table | trip advisor ranking (`frequency_tier`), how the assistant explains a system's role |
| `[fares]` | `fare_vending` table | `how_to_pay`, per-leg fares in `plan_connection` / `estimate_trip_cost` |
| `[[sources]]` | everything else | ingest pipeline — each entry is handled by the plugin named in `type` |

Built-in definitions live in `src/transit_mcp/agencies/definitions/`. A file in
`agencies/` with the same `slug` replaces the built-in one; `enabled = false` turns an
agency off without deleting it.

### Built-in source types

| `type` | Options | Writes |
| --- | --- | --- |
| `gtfs` | `url = "…"` or `manual = true` | `gtfs_*` tables, per-route/per-stop service summaries, searchable route docs, `data/gtfs/<slug>.zip` |
| `web_pages` | `pages = [{url, category, follow_children}]`, `base_url`, `max_pages`, child-link rules | searchable documents tagged with the agency |
| `wmata_api` | `include_fares` | WMATA stations, lines, alerts, elevator outages, station-to-station fares |

---

## 2. Add a new kind of data source

Write a class with `type`, `scope`, `updates_network` and `run(ctx, agency, source)`,
and register it. `src/transit_mcp/ingest/sources/wmata_api.py` is the worked example
of an agency-specific real-time API.

```python
# src/transit_mcp/ingest/sources/my_agency_api.py
from transit_mcp.ingest.sources import register_source

@register_source
class MyAgencyApi:
    type = "my_agency_api"   # what agency TOML files put in `type = "..."`
    scope = "api"            # which `ingest <scope>` runs it: api | gtfs | web
    updates_network = True   # True if it writes stops -> transfer graph is rebuilt

    def run(self, ctx, agency, source):
        opts = source.options()               # every other key from the TOML block
        data = ctx.api_http.get_bytes(opts["url"])
        # ... normalize and write with ctx.db (see db/database.py helpers) ...
        return {"records": 123}                # shown in the ingest summary
```

Then add the module to `_BUILTINS` in `src/transit_mcp/ingest/sources/__init__.py`, or
— to keep it in a separate package — declare an entry point instead:

```toml
[project.entry-points."transit_mcp.sources"]
my_agency_api = "my_package.sources:MyAgencyApi"
```

`ctx` gives you `db`, `settings`, `web_http` (polite crawler with caching),
`api_http` (rate-limited client), and `options` (`force`, `load_stop_times`, …). Raise
an exception to report failure — the pipeline records it and carries on with every
other source.

**Making a non-GTFS agency's stops part of the network:** write its stops into
`gtfs_stops` (with `feed_id = <slug>`) and its service days into
`stop_service_summary`. Transfer points, nearby-stop search and the trip planner then
include it automatically.

---

## 3. Plug in a scheduling engine

The assistant never calls an engine directly. It builds a **`TripRequest`**, hands it
to whichever engine `PLANNER_ENGINE` selects, and expects a **`TripPlan`** back. Both
are defined in `src/transit_mcp/planning/models.py`; JSON Schemas are in
[`docs/planner-schema/`](planner-schema/) (regenerate with
`transit-mcp planner schema`).

### What your engine receives

- `origin` / `destination` — already geocoded: `label`, `lat`, `lon`, and
  `nearby_stops[]` (agency, stop_id, name, distance, walk minutes, routes) within ~900 m.
- `day`, `date`, `depart_after`, `arrive_by`, `rider_category`, `max_options`.
- `context.feeds[]` — absolute `path` to each agency's **raw GTFS zip** in
  `data/gtfs/`, with fetch time and validity dates; `context.manifest` points at
  `data/gtfs/manifest.json`. These are refreshed by every ingest.

WMATA Metrorail stations appear in `nearby_stops` with `kind = "wmata"` and the WMATA
station code; WMATA's own data comes from its API rather than a GTFS zip.

### What it must return

At minimum `origin`, `destination` and `options[]`. Each option has a `summary` and
`legs[]` (`mode` = `ride` | `walk`, plus whatever you know: `agency`, `route`,
`board`, `alight`, `depart_time`, `arrive_time`, `fare`, `pay_with`, …), and ideally
`feasible` + a plain-language `feasibility`. Put anything the rider should know in
`warnings[]`. Unknown fields are allowed on legs and options.

### Option A — Python, in-process (simplest to ship)

```ini
PLANNER_ENGINE=python
PLANNER_PYTHON=engines/my_engine/engine.py:create
```

`create(settings)` returns an object with `plan(request)` (a `TripPlan` or the same
shape as a dict) and optionally `describe()`. `package.module:create` works too if
your engine is an installed package. Template: `engines/example_python/engine.py`.

### Option B — Node.js (or anything else) as a child process

```ini
PLANNER_ENGINE=process
PLANNER_COMMAND=node engines/my-engine/engine.js
```

The process is started once and kept running (so you can load GTFS into memory a
single time). It reads one JSON object per line on stdin and writes one per line on
stdout:

```text
-> {"id": 1, "method": "describe", "params": {}}
<- {"id": 1, "result": {"name": "my-engine", "version": "1.0.0"}}
-> {"id": 2, "method": "plan", "params": {...TripRequest...}}
<- {"id": 2, "result": {...TripPlan...}}
<- {"id": 2, "error": {"message": "why it failed"}}      (on failure)
```

**stdout is the reply channel** — log with `console.error`. Lines that aren't a JSON
reply with the matching `id` are ignored. If the process dies or doesn't answer
within `PLANNER_TIMEOUT_SECONDS`, it is restarted on the next request. Template:
`engines/example-js/engine.js` (zero dependencies, Node 18+).

### Option C — HTTP service

```ini
PLANNER_ENGINE=http
PLANNER_URL=http://127.0.0.1:9000
```

`POST /plan` with a TripRequest body returns a TripPlan; `GET /describe` returns
engine info.

### Developing and testing your engine

```bash
.venv\Scripts\transit-mcp.exe planner request "Reston Town Center" "King St-Old Town" --day saturday > sample-request.json
```

```bash
.venv\Scripts\transit-mcp.exe planner test "Reston Town Center" "King St-Old Town" --engine process --command "node engines/my-engine/engine.js"
```

`planner request` prints exactly what your engine will receive, built from real data.
`planner test` sends it, validates the reply against the contract, and prints the
plan — with fallback turned off so errors surface. `check-setup.bat` reports whether
the configured engine answers.

### Safety net

With `PLANNER_FALLBACK=true` (the default), if your engine errors, times out, or
returns something off-contract, riders still get an answer from the built-in
heuristic, with a warning saying so. Set it to `false` while developing if you'd
rather see failures.

### Contract versioning

`TripRequest.contract_version` / `TripPlan.contract_version` is `"1"`. New optional
fields may be added without a version change; anything that could break an existing
engine bumps it.
