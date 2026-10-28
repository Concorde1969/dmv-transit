# Data sources, licensing, and scraping etiquette

## No affiliation

This software and any site running it (such as dmvtransit.com) are **unofficial**. They
are not affiliated with, endorsed by, sponsored by, or operated by the Washington
Metropolitan Area Transit Authority (WMATA), Virginia Railway Express, Fairfax County,
Arlington County, the Alexandria Transit Company (DASH), the City of Fairfax (CUE),
Loudoun County, the Potomac and Rappahannock Transportation Commission (OmniRide),
Virginia DRPT / Virginia Breeze, or any other transit agency. Agency names and service
names are used only to identify the services described. The public site states this
in its header, welcome message, and footer.

## WMATA Developer API

Data retrieved from `api.wmata.com` is provided by the Washington Metropolitan Area
Transit Authority under its **Developer License Agreement**
(<https://developer.wmata.com/license>). Summary of how this project complies:

- A free developer API key is required and must be supplied by the operator of this
  software (`WMATA_API_KEY`). This project ships no key.
- API responses are **cached locally** for use by a language model, not rebroadcast as
  a competing real-time feed.
- WMATA is credited as the source. Every stored record keeps the originating endpoint
  URL and the fetch timestamp.
- Client-side rate limiting keeps request volume well under the Default Tier limits.

If you redistribute this software or the database it produces, review the current
Developer License Agreement yourself — terms may have changed.

## Scraping wmata.com

- `https://www.wmata.com/robots.txt` currently allows all user agents except the
  `/private/` path. This project only fetches public informational pages under
  `/ride/`, `/pay/`, and `/about/`.
- Requests are made from a descriptive `User-Agent` string (set `HTTP_USER_AGENT` in
  `.env` to include your own contact URL or email).
- A configurable crawl delay (default 1 request/second) and conditional GET
  (`ETag` / `If-Modified-Since`) minimize load.
- Scraping is limited to a **curated list of pages** (`ingest/pages.py`), not an open
  crawl of the whole site.
- Stored content keeps the page URL and retrieval timestamp for attribution, and is
  used only to ground a language model's answers — not republished as a stand-alone
  copy of WMATA's website.

## Map searches and page activity

For each map search the site keeps: when it happened, the anonymous visitor ID, the
kind of device, how the start and end were chosen (your location, a suggestion, a
shared link), the start and end rounded to about 100 meters, the nearest stop and rail
station to each, the names of landmarks, stations or areas picked from the suggestions
(never street addresses, never typed text), and the trip options shown. It also keeps
page activity: visits (the referring site's name only), address searches (how many
results, not the text), which options and panels were opened, and whether location
was allowed. All of it is kept for `CHATLOG_RETENTION_DAYS` and removed with a
visitor's chats on request (`transit-mcp chatlog delete --visitor`).

## Rider reports

Rider reports are written by visitors to the site, not by any transit agency, and are
shown as unverified. Before saving, email addresses and phone numbers are removed;
visitors are identified only by the anonymous ID described below. Each report is sent to
Anthropic's API once to check and summarize it. Reports are kept for
`REPORTS_RETENTION_DAYS` (365 by default) and can be removed with
`transit-mcp reports delete`.

## Fonts

The credit line uses Kaushan Script by Pablo Impallari and Igino Marini, licensed under
the SIL Open Font License 1.1 (`src/transit_mcp/chat/static/fonts/KaushanScript-OFL.txt`).

## Chat logs and privacy

The public site saves each conversation for review (`src/transit_mcp/chatlog.py`):
questions, answers, which data tools were used, timing, token counts, and why a
question was refused if it was. Visitors are identified only by an anonymous ID — a
keyed hash of the IP address that can't be turned back into the address — plus a
coarse device type (phone / tablet / computer). IP addresses and full browser
identifiers are not stored; the app's own request log is off and the web server's
access log masks addresses to their network. Email addresses, phone numbers and long
digit runs are removed from questions and answers before saving. Rows are deleted
after `CHATLOG_RETENTION_DAYS` (365 by default), and `transit-mcp chatlog delete`
removes a conversation or a visitor's chats on request. The site tells visitors all of
this in its footer and "About this site" section. The chat log sits on the server
disk and is included in its automatic snapshots.

## GTFS feeds and Transitland

Routes, stops and schedules come only from GTFS static feeds. WMATA's rail and bus feeds
are used under the WMATA Developer License Agreement (above); the feeds for VRE,
Fairfax Connector, ART, DASH, CUE, Loudoun County Transit, OmniRide, Virginia Breeze,
the Maryland Transit Administration (local and commuter bus, Metro SubwayLink, Light
RailLink, MARC), Annapolis Transit, Harbor Connector, Potomac Valley Transit, Amtrak,
FlixBus and the combined intercity-bus feed are published by those operators (or their
sponsors, e.g. Virginia DRPT) under the GTFS specification, each under its own terms.
Feeds are located through [Transitland](https://www.transit.land/) (its public
`transitland-atlas` registry first, its API only as a fallback) and downloaded from the
official URL Transitland lists; Transitland's own copy is used only when that fails and
Transitland's license data allows redistribution. Fairfax Connector's GTFS-realtime
service alerts are read from the URL Transitland lists. This project:

- caches each feed locally and records its `feed_publisher_name` / `feed_publisher_url`
  from `feed_info.txt` for attribution;
- keeps an unmodified copy of each feed zip in `data/gtfs/` (with a `manifest.json`
  recording its source URL and fetch time) so a local scheduling engine can read it;
  this archive is a local cache and is not redistributed;
- fetches each feed at most once per ingest run;
- loads `shapes.txt` simplified (to about 10 m) only to draw routes on the map;
- scrapes a small set of each agency's public fare / how-to-pay pages under the same
  rules as the WMATA pages above (rate-limited, descriptive User-Agent, source URL and
  timestamp stored).

## Live departure times

The map planner reads live departure times from each agency's own GTFS-realtime
TripUpdates feed at the URL Transitland lists (WMATA's with the operator's WMATA key),
downloading each feed at most every 30 seconds while someone is planning a trip, and
from Transitland's departures API (Explorer plan, non-commercial use) for feeds it
can't read directly. Live times are shown to the rider who asked, not stored or
republished as a feed.

## Geocoding and the map

- Typed addresses are turned into map points by [geocode.maps.co](https://geocode.maps.co/)
  (free plan, non-commercial use), called from the server only; results are cached for
  24 hours.
- The map shows data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)
  (Open Database License), from a regional extract of the
  [Protomaps](https://protomaps.com/) basemap build hosted on our own server. The map
  credits OpenStreetMap on screen.
- Map software: [MapLibre GL JS](https://github.com/maplibre/maplibre-gl-js) (BSD-3-Clause),
  [PMTiles](https://github.com/protomaps/PMTiles) (BSD-3-Clause) and the Protomaps
  basemap styles, fonts and sprites (BSD-3-Clause / OFL), vendored at pinned versions
  under `src/transit_mcp/chat/static/map/`.
- **Location privacy.** "My location" uses the browser's location only after the
  visitor allows it, and only to find nearby stops. The trip search log keeps start and
  end points rounded to about 100 meters, never typed addresses or exact locations.
- Before any commercial use of the site, check the Transitland and geocode.maps.co plan
  terms — the current plans are for non-commercial use.

## Editorial content

Agency "role", "strengths", and fare-vending summaries in this project are editorial
descriptions written for this tool, not official statements from any agency. Fares and
schedules change — the tool cites each agency's own fare page and flags stale data.

## No affiliation

This project is not affiliated with, endorsed by, or sponsored by WMATA or any of the
regional transit agencies listed above.
