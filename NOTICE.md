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

## Regional GTFS feeds

GTFS static feeds for VRE, Fairfax Connector, ART, DASH, CUE, Loudoun County Transit,
OmniRide, and Virginia Breeze are published by those agencies (or their sponsors, e.g.
Virginia DRPT) for redistribution under the GTFS specification. This project:

- caches each feed locally and records its `feed_publisher_name` / `feed_publisher_url`
  from `feed_info.txt` for attribution;
- keeps an unmodified copy of each feed zip in `data/gtfs/` (with a `manifest.json`
  recording its source URL and fetch time) so a local scheduling engine can read it;
  this archive is a local cache and is not redistributed;
- fetches each feed at most once per ingest run;
- does not load `shapes.txt`;
- scrapes a small set of each agency's public fare / how-to-pay pages under the same
  rules as the WMATA pages above (rate-limited, descriptive User-Agent, source URL and
  timestamp stored).

Agency "role", "strengths", and fare-vending summaries in this project are editorial
descriptions written for this tool, not official statements from any agency. Fares and
schedules change — the tool cites each agency's own fare page and flags stale data.

## No affiliation

This project is not affiliated with, endorsed by, or sponsored by WMATA or any of the
regional transit agencies listed above.
