# Manually-dropped GTFS feeds

Most regional feeds download automatically during `transit-mcp ingest gtfs`. Two
agencies block automated download and must be placed here by hand:

| slug | where to get the zip |
| --- | --- |
| `vre` | <https://www.vre.org/> → GTFS / developer data (Cloudflare blocks scripts) |
| `cue` | <https://www.fairfaxva.gov/> CUE Bus page → GTFS download |

Save each as:

```
feeds/vre/google_transit.zip
feeds/cue/google_transit.zip
```

Then double-click `refresh-data.bat` (or run `transit-mcp ingest gtfs --only vre,cue`).

A local `google_transit.zip` (also accepted: `<slug>.zip` or `gtfs.zip`) always wins
over an agency's download URL, so you can also use this folder to pin a specific
feed version for any agency. An agency's file opts into manual download with
`manual = true` on its `gtfs` source (see `agencies/_template.toml`). This directory
is git-ignored except for this file.
