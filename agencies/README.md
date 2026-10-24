# Agencies

Every transit agency the assistant knows about is described by one TOML file. No
code changes are needed to add, change, or remove an agency.

| Where | What |
| --- | --- |
| `src/transit_mcp/agencies/definitions/*.toml` | Built-in agencies (WMATA, VRE, Fairfax Connector, ART, DASH, CUE, Loudoun, OmniRide, Virginia Breeze) |
| this folder (`agencies/*.toml`) | Your additions. A file with a new slug **adds** an agency; one with an existing slug **replaces** the built-in definition. |

Files starting with `_` are ignored.

## Add an agency

```bash
copy agencies\_template.toml agencies\my-agency.toml
```

Edit it, then:

```bash
.venv\Scripts\transit-mcp.exe agencies --config
```

```bash
.venv\Scripts\transit-mcp.exe ingest --only my-agency
```

The first command validates every file and points to the exact field that is
wrong. The second loads just that agency. From then on it is included in
`refresh-data.bat`, the trip planner, `list_agencies`, and every search.

## Turn an agency off

Set `enabled = false` in its file (copy a built-in file here first to override it).

See [docs/EXTENDING.md](../docs/EXTENDING.md) for new kinds of data sources.
