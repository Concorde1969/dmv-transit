# Manually-dropped GTFS feeds

Schedules download automatically during `transit-mcp ingest gtfs` — found through
Transitland for every agency it lists. One agency is neither in Transitland nor open
to automated download, so its feed must be placed here by hand:

| slug | where to get the zip |
| --- | --- |
| `cue` | <https://www.fairfaxva.gov/> CUE Bus page → GTFS download |

Save it as:

```
feeds/cue/google_transit.zip
```

Then double-click `refresh-data.bat` (or run `transit-mcp ingest gtfs --only cue`).

A local `google_transit.zip` (also accepted: `<slug>.zip` or `gtfs.zip`) is the last
resort: it is used only if Transitland and the agency's own download URL both fail, so
it can't silently pin an old version over a current published feed. An agency's file
opts into manual download with `manual = true` on its `gtfs` source (see
`agencies/_template.toml`). This directory is git-ignored except for this file.
