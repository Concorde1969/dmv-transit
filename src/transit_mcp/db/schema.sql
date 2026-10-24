-- transit-mcp schema. Idempotent: safe to run repeatedly.
-- SQLite >= 3.35 with the FTS5 extension (bundled with CPython).
-- Existing v1 databases are upgraded by db.database._apply_migrations().

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Schema version marker. Bump + add a migration step when changing existing tables.
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO schema_meta (key, value) VALUES ('version', '4')
    ON CONFLICT(key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Provenance: one row per fetched URL (API endpoint, web page, or GTFS feed).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY,
    url             TEXT NOT NULL UNIQUE,
    source_type     TEXT NOT NULL CHECK (source_type IN ('api', 'web')),
    category        TEXT NOT NULL,
    feed_id         TEXT,                       -- agency slug, NULL = WMATA / region-wide
    http_status     INTEGER,
    etag            TEXT,
    last_modified   TEXT,
    content_hash    TEXT,
    first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_changed_at TEXT
);

-- ---------------------------------------------------------------------------
-- Narrative content (scraped pages + synthesized structured summaries).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY,
    source_id      INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    category       TEXT NOT NULL,
    feed_id        TEXT,                        -- agency slug, NULL = WMATA / region-wide
    title          TEXT NOT NULL,
    summary        TEXT,
    body_markdown  TEXT NOT NULL,
    url            TEXT NOT NULL,
    effective_date TEXT,
    expires_at     TEXT,
    retrieved_at   TEXT NOT NULL DEFAULT (datetime('now')),
    raw_hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_documents_feed ON documents(feed_id);

CREATE TABLE IF NOT EXISTS document_chunks (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    heading_path TEXT NOT NULL DEFAULT '',
    text         TEXT NOT NULL,
    char_len     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON document_chunks(document_id);

-- Full-text index over chunks. External-content table backed by document_chunks.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5 (
    text,
    title UNINDEXED,
    category UNINDEXED,
    content = 'document_chunks',
    content_rowid = 'id',
    tokenize = 'porter unicode61'
);

-- Keep chunks_fts in sync with document_chunks.
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON document_chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON document_chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON document_chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

-- ---------------------------------------------------------------------------
-- Real-time service disruptions (WMATA API).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id                INTEGER PRIMARY KEY,
    source_id         INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    provider_alert_id TEXT NOT NULL UNIQUE,
    mode              TEXT NOT NULL CHECK (mode IN ('rail', 'bus', 'elevator_escalator')),
    headline          TEXT NOT NULL,
    description       TEXT,
    affected_lines    TEXT NOT NULL DEFAULT '[]',   -- JSON array
    affected_stations TEXT NOT NULL DEFAULT '[]',   -- JSON array
    feed_id           TEXT NOT NULL DEFAULT 'wmata', -- agency slug
    affected_routes   TEXT NOT NULL DEFAULT '[]',   -- JSON array of GTFS route_ids
    affected_stops    TEXT NOT NULL DEFAULT '[]',   -- JSON array of GTFS stop_ids
    url               TEXT,
    severity          TEXT,
    starts_at         TEXT,
    ends_at           TEXT,
    updated_at        TEXT,
    raw_json          TEXT NOT NULL,
    seen_at           TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(mode, resolved_at);
CREATE INDEX IF NOT EXISTS idx_alerts_feed ON alerts(feed_id, resolved_at);

-- ---------------------------------------------------------------------------
-- Stations / lines (WMATA API).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stations (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    lat             REAL,
    lon             REAL,
    line_codes      TEXT NOT NULL DEFAULT '[]',  -- JSON array
    address_json    TEXT NOT NULL DEFAULT '{}',  -- JSON object
    has_parking     INTEGER NOT NULL DEFAULT 0,
    together_codes  TEXT NOT NULL DEFAULT '[]',  -- JSON array
    source_id       INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    raw_json        TEXT NOT NULL DEFAULT '{}',
    retrieved_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_stations_latlon ON stations(lat, lon);

-- Station order along each Metrorail line (from WMATA's rail GTFS; WMATA's jPath
-- endpoint omits shared stations on some lines, so it can't be used for transfers).
CREATE TABLE IF NOT EXISTS rail_line_stops (
    line_code    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    PRIMARY KEY (line_code, seq)
);
CREATE INDEX IF NOT EXISTS idx_rail_line_stops_station ON rail_line_stops(station_code);

-- Alternate names riders use for a stop or station ("DCA", "Reagan", "IAD").
-- Seeded from each agency definition's [aliases] table.
CREATE TABLE IF NOT EXISTS place_aliases (
    alias   TEXT PRIMARY KEY,     -- lowercase
    feed_id TEXT NOT NULL,
    stop_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lines (
    code               TEXT PRIMARY KEY,
    display_name       TEXT NOT NULL,
    color_hex          TEXT,
    start_station_code TEXT,
    end_station_code   TEXT,
    source_id          INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    raw_json           TEXT NOT NULL DEFAULT '{}',
    retrieved_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Elevator / escalator unit status (WMATA API).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS unit_status (
    id                   INTEGER PRIMARY KEY,
    station_code         TEXT NOT NULL,
    unit_type            TEXT NOT NULL CHECK (unit_type IN ('elevator', 'escalator')),
    unit_name            TEXT NOT NULL,
    location_description  TEXT,
    symptom              TEXT,
    out_of_service       INTEGER NOT NULL DEFAULT 1,
    date_out_of_service   TEXT,
    estimated_return     TEXT,
    updated_at           TEXT,
    source_id            INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    raw_json             TEXT NOT NULL,
    seen_at              TEXT NOT NULL DEFAULT (datetime('now')),
    cleared_at           TEXT,
    UNIQUE (station_code, unit_name)
);
CREATE INDEX IF NOT EXISTS idx_unit_status_active ON unit_status(station_code, cleared_at);

-- ---------------------------------------------------------------------------
-- Station-to-station rail fares (WMATA API).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fares (
    id                   INTEGER PRIMARY KEY,
    origin_code          TEXT NOT NULL,
    dest_code            TEXT NOT NULL,
    peak_fare            REAL,
    offpeak_fare         REAL,
    senior_disabled_fare REAL,
    rail_time_min        INTEGER,
    source_id            INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    raw_json             TEXT NOT NULL,
    retrieved_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (origin_code, dest_code)
);

-- ---------------------------------------------------------------------------
-- Ingestion run log.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    scope       TEXT NOT NULL CHECK (scope IN ('api', 'web', 'gtfs', 'all')),
    status      TEXT NOT NULL DEFAULT 'error' CHECK (status IN ('ok', 'partial', 'error')),
    stats_json  TEXT NOT NULL DEFAULT '{}',
    error_text  TEXT
);

-- ===========================================================================
-- Regional GTFS: complementary agencies (VRE, Fairfax Connector, ART, DASH,
-- CUE, Loudoun, OmniRide, Virginia Breeze). Every row is namespaced by
-- feed_id (the agency slug). shapes.txt is intentionally not loaded.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS gtfs_feeds (
    feed_id         TEXT PRIMARY KEY,
    agency_name     TEXT NOT NULL,
    mode            TEXT,
    source_url      TEXT,
    is_local        INTEGER NOT NULL DEFAULT 0,
    feed_hash       TEXT,
    feed_start_date TEXT,
    feed_end_date   TEXT,
    publisher_name  TEXT,
    publisher_url   TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('loaded', 'pending', 'error')),
    error_text      TEXT,
    fetched_at      TEXT,
    last_changed_at TEXT,
    transitland_id  TEXT,             -- Transitland Onestop ID, when listed there
    fetched_via     TEXT,             -- transitland | transitland-listed-url | agency-url | local file
    index_stops     INTEGER NOT NULL DEFAULT 1  -- 0: stops not used for place search/links
);

CREATE TABLE IF NOT EXISTS gtfs_agencies (
    feed_id     TEXT NOT NULL,
    agency_id   TEXT NOT NULL DEFAULT '',
    name        TEXT,
    url         TEXT,
    timezone    TEXT,
    lang        TEXT,
    phone       TEXT,
    fare_url    TEXT,
    PRIMARY KEY (feed_id, agency_id)
);

CREATE TABLE IF NOT EXISTS gtfs_routes (
    feed_id     TEXT NOT NULL,
    route_id    TEXT NOT NULL,
    agency_id   TEXT,
    short_name  TEXT,
    long_name   TEXT,
    description TEXT,
    route_type  INTEGER,
    route_url   TEXT,
    color       TEXT,
    text_color  TEXT,
    PRIMARY KEY (feed_id, route_id)
);
CREATE INDEX IF NOT EXISTS idx_gtfs_routes_feed ON gtfs_routes(feed_id);

CREATE TABLE IF NOT EXISTS gtfs_stops (
    feed_id             TEXT NOT NULL,
    stop_id             TEXT NOT NULL,
    code                TEXT,
    name                TEXT,
    description         TEXT,
    lat                 REAL,
    lon                 REAL,
    zone_id             TEXT,
    location_type       INTEGER,
    parent_station      TEXT,
    wheelchair_boarding INTEGER,
    PRIMARY KEY (feed_id, stop_id)
);
CREATE INDEX IF NOT EXISTS idx_gtfs_stops_latlon ON gtfs_stops(lat, lon);
CREATE INDEX IF NOT EXISTS idx_gtfs_stops_name ON gtfs_stops(name);

CREATE TABLE IF NOT EXISTS gtfs_trips (
    feed_id                TEXT NOT NULL,
    trip_id                TEXT NOT NULL,
    route_id               TEXT,
    service_id             TEXT,
    headsign               TEXT,
    direction_id           INTEGER,
    block_id               TEXT,
    shape_id               TEXT,
    wheelchair_accessible   INTEGER,
    PRIMARY KEY (feed_id, trip_id)
);
CREATE INDEX IF NOT EXISTS idx_gtfs_trips_route ON gtfs_trips(feed_id, route_id);
CREATE INDEX IF NOT EXISTS idx_gtfs_trips_service ON gtfs_trips(feed_id, service_id);

CREATE TABLE IF NOT EXISTS gtfs_stop_times (
    feed_id        TEXT NOT NULL,
    trip_id        TEXT NOT NULL,
    stop_id        TEXT NOT NULL,
    stop_sequence  INTEGER NOT NULL,
    arrival_time   TEXT,
    departure_time TEXT,
    pickup_type    INTEGER,
    drop_off_type  INTEGER,
    stop_headsign  TEXT
);
CREATE INDEX IF NOT EXISTS idx_gtfs_stop_times_stop ON gtfs_stop_times(feed_id, stop_id);
CREATE INDEX IF NOT EXISTS idx_gtfs_stop_times_trip ON gtfs_stop_times(feed_id, trip_id);

CREATE TABLE IF NOT EXISTS gtfs_calendar (
    feed_id    TEXT NOT NULL,
    service_id TEXT NOT NULL,
    monday     INTEGER NOT NULL DEFAULT 0,
    tuesday    INTEGER NOT NULL DEFAULT 0,
    wednesday  INTEGER NOT NULL DEFAULT 0,
    thursday   INTEGER NOT NULL DEFAULT 0,
    friday     INTEGER NOT NULL DEFAULT 0,
    saturday   INTEGER NOT NULL DEFAULT 0,
    sunday     INTEGER NOT NULL DEFAULT 0,
    start_date TEXT,
    end_date   TEXT,
    PRIMARY KEY (feed_id, service_id)
);

CREATE TABLE IF NOT EXISTS gtfs_calendar_dates (
    feed_id        TEXT NOT NULL,
    service_id     TEXT NOT NULL,
    date           TEXT NOT NULL,
    exception_type INTEGER NOT NULL,
    PRIMARY KEY (feed_id, service_id, date)
);

CREATE TABLE IF NOT EXISTS gtfs_frequencies (
    feed_id      TEXT NOT NULL,
    trip_id      TEXT NOT NULL,
    start_time   TEXT NOT NULL,
    end_time     TEXT NOT NULL,
    headway_secs INTEGER NOT NULL,
    exact_times  INTEGER,
    PRIMARY KEY (feed_id, trip_id, start_time)
);

CREATE TABLE IF NOT EXISTS gtfs_transfers (
    feed_id           TEXT NOT NULL,
    from_stop_id      TEXT,
    to_stop_id        TEXT,
    transfer_type     INTEGER,
    min_transfer_time INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gtfs_transfers_feed ON gtfs_transfers(feed_id);

CREATE TABLE IF NOT EXISTS gtfs_fare_attributes (
    feed_id           TEXT NOT NULL,
    fare_id           TEXT NOT NULL,
    price             REAL,
    currency_type     TEXT,
    payment_method    INTEGER,
    transfers         TEXT,
    agency_id         TEXT,
    transfer_duration INTEGER,
    PRIMARY KEY (feed_id, fare_id)
);

CREATE TABLE IF NOT EXISTS gtfs_fare_rules (
    feed_id        TEXT NOT NULL,
    fare_id        TEXT NOT NULL,
    route_id       TEXT,
    origin_id      TEXT,
    destination_id TEXT,
    contains_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_gtfs_fare_rules_feed ON gtfs_fare_rules(feed_id);

-- GTFS-Fares v2 (loaded only when the file is present in the feed).
CREATE TABLE IF NOT EXISTS gtfs_fare_products (
    feed_id           TEXT NOT NULL,
    fare_product_id   TEXT NOT NULL,
    fare_product_name TEXT,
    fare_media_id     TEXT,
    amount            REAL,
    currency          TEXT,
    PRIMARY KEY (feed_id, fare_product_id, fare_media_id)
);

CREATE TABLE IF NOT EXISTS gtfs_fare_media (
    feed_id         TEXT NOT NULL,
    fare_media_id   TEXT NOT NULL,
    fare_media_name TEXT,
    fare_media_type INTEGER,
    PRIMARY KEY (feed_id, fare_media_id)
);

CREATE TABLE IF NOT EXISTS gtfs_rider_categories (
    feed_id              TEXT NOT NULL,
    rider_category_id    TEXT NOT NULL,
    rider_category_name  TEXT,
    is_default_fare_category INTEGER,
    eligibility_url      TEXT,
    PRIMARY KEY (feed_id, rider_category_id)
);

CREATE TABLE IF NOT EXISTS gtfs_ticketing_deep_links (
    feed_id                    TEXT NOT NULL,
    ticketing_deep_link_id     TEXT NOT NULL,
    web_url                    TEXT,
    android_intent_uri         TEXT,
    ios_universal_link_url     TEXT,
    PRIMARY KEY (feed_id, ticketing_deep_link_id)
);

CREATE TABLE IF NOT EXISTS gtfs_ticketing_identifiers (
    feed_id                 TEXT NOT NULL,
    stop_id                 TEXT,
    ticketing_stop_id       TEXT,
    ticketing_deep_link_id  TEXT
);

-- ---------------------------------------------------------------------------
-- Derived: per-route and per-stop service pictures (feasibility core).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_service_summary (
    feed_id            TEXT NOT NULL,
    route_id           TEXT NOT NULL,
    service_days       TEXT,
    runs_weekday       INTEGER NOT NULL DEFAULT 0,
    runs_saturday      INTEGER NOT NULL DEFAULT 0,
    runs_sunday        INTEGER NOT NULL DEFAULT 0,
    first_departure    TEXT,
    last_departure     TEXT,
    trips_per_week     INTEGER NOT NULL DEFAULT 0,
    typical_headway_min INTEGER,
    num_stops          INTEGER,
    first_stop_name    TEXT,
    last_stop_name     TEXT,
    computed_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (feed_id, route_id)
);

CREATE TABLE IF NOT EXISTS stop_service_summary (
    feed_id          TEXT NOT NULL,
    stop_id          TEXT NOT NULL,
    stop_name        TEXT,
    lat              REAL,
    lon              REAL,
    runs_mon INTEGER NOT NULL DEFAULT 0,
    runs_tue INTEGER NOT NULL DEFAULT 0,
    runs_wed INTEGER NOT NULL DEFAULT 0,
    runs_thu INTEGER NOT NULL DEFAULT 0,
    runs_fri INTEGER NOT NULL DEFAULT 0,
    runs_sat INTEGER NOT NULL DEFAULT 0,
    runs_sun INTEGER NOT NULL DEFAULT 0,
    weekday_first    TEXT,
    weekday_last     TEXT,
    saturday_first   TEXT,
    saturday_last    TEXT,
    sunday_first     TEXT,
    sunday_last      TEXT,
    trips_per_week   INTEGER NOT NULL DEFAULT 0,
    route_ids        TEXT NOT NULL DEFAULT '[]',
    serves_weekend   INTEGER NOT NULL DEFAULT 0,
    evening_service  INTEGER NOT NULL DEFAULT 0,
    computed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (feed_id, stop_id)
);

-- ---------------------------------------------------------------------------
-- Cross-system transfer graph (geographic + in-feed transfers.txt).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stop_links (
    id                INTEGER PRIMARY KEY,
    a_kind            TEXT NOT NULL CHECK (a_kind IN ('gtfs', 'wmata')),
    a_feed            TEXT NOT NULL,
    a_id              TEXT NOT NULL,
    a_name            TEXT,
    a_lat             REAL,
    a_lon             REAL,
    b_kind            TEXT NOT NULL CHECK (b_kind IN ('gtfs', 'wmata')),
    b_feed            TEXT NOT NULL,
    b_id              TEXT NOT NULL,
    b_name            TEXT,
    b_lat             REAL,
    b_lon             REAL,
    distance_m        INTEGER NOT NULL,
    walk_min          INTEGER NOT NULL,
    link_source       TEXT NOT NULL DEFAULT 'proximity'
                      CHECK (link_source IN ('proximity', 'gtfs_transfers')),
    overlap_days      TEXT,
    both_serve_weekend INTEGER NOT NULL DEFAULT 0,
    computed_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (a_feed, a_id, b_feed, b_id)
);
CREATE INDEX IF NOT EXISTS idx_stop_links_a ON stop_links(a_feed, a_id);
CREATE INDEX IF NOT EXISTS idx_stop_links_b ON stop_links(b_feed, b_id);
CREATE INDEX IF NOT EXISTS idx_stop_links_aname ON stop_links(a_name);
CREATE INDEX IF NOT EXISTS idx_stop_links_bname ON stop_links(b_name);

-- ---------------------------------------------------------------------------
-- Curated: agency role/strengths and how-to-pay. Seeded from ingest/gtfs/feeds.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agency_profiles (
    feed_id        TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    mode           TEXT,
    role           TEXT,
    strengths      TEXT,
    weaknesses     TEXT,
    service_area   TEXT,
    connects_to    TEXT NOT NULL DEFAULT '[]',   -- JSON array of hub names
    frequency_tier TEXT CHECK (frequency_tier IN
                    ('frequent', 'peak', 'infrequent', 'intercity')),
    park_and_ride  INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fare_vending (
    feed_id               TEXT PRIMARY KEY,
    base_fare             REAL,
    reduced_fare          REAL,
    currency              TEXT NOT NULL DEFAULT 'USD',
    payment_methods       TEXT NOT NULL DEFAULT '[]',  -- JSON array
    where_to_buy          TEXT NOT NULL DEFAULT '[]',  -- JSON array
    mobile_app_name       TEXT,
    app_ios_url           TEXT,
    app_android_url       TEXT,
    accepts_smartrip      INTEGER NOT NULL DEFAULT 0,
    accepts_contactless   INTEGER NOT NULL DEFAULT 0,
    cash_onboard          INTEGER NOT NULL DEFAULT 0,
    exact_change_required INTEGER NOT NULL DEFAULT 0,
    transfer_policy       TEXT,
    fare_page_url         TEXT,
    notes                 TEXT,
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
