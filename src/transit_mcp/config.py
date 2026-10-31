"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings.

    Values come from (in order of precedence) real environment variables, then a
    local ``.env`` file, then the defaults below.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # WMATA Developer API: alerts, stations, fares, and WMATA's GTFS feeds.
    wmata_api_key: str | None = Field(default=None, alias="WMATA_API_KEY")
    # Optional (free at transit.land): lets GTFS feeds come from Transitland's own
    # copies and API. Without it, Transitland's public feed registry is used.
    transitland_api_key: str | None = Field(default=None, alias="TRANSITLAND_API_KEY")
    # Transitland REST calls allowed per month (Explorer plan: 5,000; keep a margin).
    transitland_monthly_budget: int = Field(default=4500, alias="TRANSITLAND_MONTHLY_BUDGET")
    # Address search on the map (server-side only). Google's Geocoding API is used when
    # its key is set; otherwise geocode.maps.co.
    google_geocoding_api_key: str | None = Field(default=None,
                                                 alias="GOOGLE_GEOCODING_API_KEY")
    # Google bills per request: at most this many a month (searches and reverse lookups).
    google_geocoding_monthly_budget: int = Field(default=9000,
                                                 alias="GOOGLE_GEOCODING_MONTHLY_BUDGET")
    # Suggestions as the rider types come from Google Places Autocomplete (same key;
    # "Places API (New)" must be enabled for it), else from the Geocoding API.
    google_places_monthly_budget: int = Field(default=9000,
                                              alias="GOOGLE_PLACES_MONTHLY_BUDGET")
    geocode_api_key: str | None = Field(default=None, alias="GEOCODE_API_KEY")

    # Storage
    db_path: Path = Field(default=Path("./transit.db"), alias="TRANSIT_MCP_DB")
    cache_dir: Path = Field(default=Path("./.cache"), alias="TRANSIT_MCP_CACHE_DIR")

    # Crawl politeness
    crawl_delay_seconds: float = Field(default=1.0, alias="CRAWL_DELAY_SECONDS")
    http_user_agent: str = Field(
        default="transit-mcp/0.1 (+https://github.com/your-org/transit-mcp)",
        alias="HTTP_USER_AGENT",
    )
    http_timeout_seconds: float = Field(default=30.0, alias="HTTP_TIMEOUT_SECONDS")

    # Conservative client-side cap for the WMATA API "Default Tier".
    api_requests_per_second: float = Field(default=8.0, alias="API_REQUESTS_PER_SECOND")

    # Freshness threshold surfaced in retrieval responses.
    stale_after_hours: int = Field(default=24, alias="STALE_AFTER_HOURS")

    # --- Agencies (data-driven; see agencies/README.md) ---
    # Extra agency definitions that add to / override the built-in ones by slug.
    agencies_dir: Path = Field(default=Path("./agencies"), alias="AGENCIES_DIR")

    # --- Regional GTFS / connections ---
    feeds_dir: Path = Field(default=Path("./feeds"), alias="FEEDS_DIR")
    # Every loaded GTFS zip is archived here for scheduling engines to read.
    gtfs_archive_dir: Path = Field(default=Path("./data/gtfs"), alias="GTFS_ARCHIVE_DIR")
    gtfs_load_stop_times: bool = Field(default=True, alias="GTFS_LOAD_STOP_TIMES")
    transfer_link_radius_m: int = Field(default=250, alias="TRANSFER_LINK_RADIUS_M")
    nearby_search_radius_m: int = Field(default=400, alias="NEARBY_SEARCH_RADIUS_M")
    walk_speed_m_per_min: float = Field(default=80.0, alias="WALK_SPEED_M_PER_MIN")

    # --- Trip planning / scheduling engine (see docs/EXTENDING.md) ---
    # heuristic | python | process | http
    planner_engine: str = Field(default="heuristic", alias="PLANNER_ENGINE")
    # python: "package.module:factory" returning an object with plan()/describe()
    planner_python: str | None = Field(default=None, alias="PLANNER_PYTHON")
    # process: command line for a long-lived child speaking JSON lines on stdio
    planner_command: str | None = Field(default=None, alias="PLANNER_COMMAND")
    # http: base URL exposing POST /plan and GET /describe
    planner_url: str | None = Field(default=None, alias="PLANNER_URL")
    planner_timeout_seconds: float = Field(default=20.0, alias="PLANNER_TIMEOUT_SECONDS")
    # Fall back to the built-in heuristic if the engine errors or times out.
    planner_fallback: bool = Field(default=True, alias="PLANNER_FALLBACK")

    # --- Chat frontend ---
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-5", alias="ANTHROPIC_MODEL")
    chat_max_tokens: int = Field(default=2048, alias="CHAT_MAX_TOKENS")
    chat_max_tool_iterations: int = Field(default=10, alias="CHAT_MAX_TOOL_ITERATIONS")
    chat_host: str = Field(default="127.0.0.1", alias="CHAT_HOST")
    chat_port: int = Field(default=8765, alias="CHAT_PORT")

    # --- Public deployment ---
    # production = generic error messages and a minimal /api/health.
    app_env: str = Field(default="development", alias="APP_ENV")
    # True behind a reverse proxy (Caddy): read the visitor IP from X-Forwarded-For.
    trust_proxy: bool = Field(default=False, alias="TRUST_PROXY")
    # Abuse / cost guards for the chat endpoint.
    chat_max_message_chars: int = Field(default=1000, alias="CHAT_MAX_MESSAGE_CHARS")
    chat_rate_per_minute: int = Field(default=6, alias="CHAT_RATE_PER_MINUTE")
    chat_rate_per_day: int = Field(default=60, alias="CHAT_RATE_PER_DAY")
    chat_daily_limit: int = Field(default=1500, alias="CHAT_DAILY_LIMIT")
    chat_max_turns: int = Field(default=20, alias="CHAT_MAX_TURNS")
    chat_history_turns: int = Field(default=6, alias="CHAT_HISTORY_TURNS")
    chat_session_ttl_minutes: int = Field(default=120, alias="CHAT_SESSION_TTL_MINUTES")
    chat_max_sessions: int = Field(default=2000, alias="CHAT_MAX_SESSIONS")

    # --- Leave-now trip planner for the map (planning/live.py) ---
    # Self-hosted basemap (Protomaps extract; see deploy/map/fetch-basemap.sh).
    map_tiles_path: Path = Field(default=Path("./data/map/dmv.pmtiles"), alias="MAP_TILES_PATH")
    # Start/end stops: each agency's closest stops, up to about a mile's walk.
    trip_max_walk_m: int = Field(default=1600, alias="TRIP_MAX_WALK_M")
    trip_stops_per_agency: int = Field(default=25, alias="TRIP_STOPS_PER_AGENCY")
    # Transfers are always between stops < 0.3 km apart. Options whose transfer waits
    # are all this short come first; longer waits (up to the max) only when nothing better.
    trip_short_transfer_min: int = Field(default=10, alias="TRIP_SHORT_TRANSFER_MIN")
    trip_max_transfer_wait_min: int = Field(default=120, alias="TRIP_MAX_TRANSFER_WAIT_MIN")
    # Departures in this many minutes are offered.
    trip_window_min: int = Field(default=180, alias="TRIP_WINDOW_MIN")
    # Most changes in one trip. Two-change trips are searched only when no trip with
    # fewer changes works.
    trip_max_transfers: int = Field(default=2, alias="TRIP_MAX_TRANSFERS")
    # Gentle walking pace (m/min) for walk times shown to riders.
    trip_walk_m_per_min: float = Field(default=70.0, alias="TRIP_WALK_M_PER_MIN")
    trip_live_cache_s: int = Field(default=60, alias="TRIP_LIVE_CACHE_S")
    # Most Transitland departures calls one search may make (Explorer plan budget).
    trip_max_live_calls: int = Field(default=6, alias="TRIP_MAX_LIVE_CALLS")
    trip_rate_per_minute: int = Field(default=10, alias="TRIP_RATE_PER_MINUTE")
    trip_rate_per_day: int = Field(default=200, alias="TRIP_RATE_PER_DAY")

    # --- Chat log (questions, answers, diagnostics; see chatlog.py) ---
    chatlog_enabled: bool = Field(default=True, alias="CHATLOG_ENABLED")
    chatlog_db: Path = Field(default=Path("./chatlog.db"), alias="CHATLOG_DB")
    chatlog_retention_days: int = Field(default=365, alias="CHATLOG_RETENTION_DAYS")
    # Remove email addresses, phone numbers and long digit runs before storing.
    chatlog_redact: bool = Field(default=True, alias="CHATLOG_REDACT")
    # Key for anonymous visitor IDs; if empty, a random one is kept in the chat log.
    chatlog_salt: str | None = Field(default=None, alias="CHATLOG_SALT")

    # --- Rider reports (crowdsourced delays and problems; see reports.py) ---
    reports_enabled: bool = Field(default=True, alias="REPORTS_ENABLED")
    # Small, fast model that checks and structures each report.
    reports_model: str = Field(default="claude-haiku-5-5", alias="REPORTS_MODEL")
    # Reports appear on matching trips for this many hours after they're sent.
    reports_show_hours: int = Field(default=3, alias="REPORTS_SHOW_HOURS")
    reports_retention_days: int = Field(default=365, alias="REPORTS_RETENTION_DAYS")
    reports_rate_per_minute: int = Field(default=2, alias="REPORTS_RATE_PER_MINUTE")
    reports_rate_per_day: int = Field(default=10, alias="REPORTS_RATE_PER_DAY")
    reports_daily_limit: int = Field(default=300, alias="REPORTS_DAILY_LIMIT")  # site-wide

    # --- Accounts (saved hotel, home and places to visit; see accounts.py) ---
    accounts_enabled: bool = Field(default=True, alias="ACCOUNTS_ENABLED")
    accounts_db: Path = Field(default=Path("./accounts.db"), alias="ACCOUNTS_DB")
    # Key for sign-in code hashes; if empty, a random one is kept in the accounts DB.
    accounts_secret: str | None = Field(default=None, alias="ACCOUNTS_SECRET")
    accounts_max_places: int = Field(default=50, alias="ACCOUNTS_MAX_PLACES")
    # Accounts nobody has used for this long are deleted with their places.
    accounts_inactive_days: int = Field(default=730, alias="ACCOUNTS_INACTIVE_DAYS")
    # Sign-in code emails the whole site may send a day.
    accounts_daily_emails: int = Field(default=500, alias="ACCOUNTS_DAILY_EMAILS")
    # Outgoing mail for sign-in codes (Amazon SES SMTP on the public site).
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_username: str | None = Field(default=None, alias="SMTP_USERNAME")
    smtp_password: str | None = Field(default=None, alias="SMTP_PASSWORD")
    mail_from: str = Field(default="DMV Transit <signin@dmvtransit.com>", alias="MAIL_FROM")

    # --- The MCP server at a web address (`transit-mcp serve --http`; server/http.py) ---
    mcp_http_host: str = Field(default="127.0.0.1", alias="MCP_HTTP_HOST")
    mcp_http_port: int = Field(default=8766, alias="MCP_HTTP_PORT")
    mcp_keys_db: Path = Field(default=Path("./mcp_keys.db"), alias="MCP_KEYS_DB")
    # Requests each API key may make.
    mcp_rate_per_minute: int = Field(default=30, alias="MCP_RATE_PER_MINUTE")
    mcp_rate_per_day: int = Field(default=300, alias="MCP_RATE_PER_DAY")

    # --- Scheduled data refresh (`transit-mcp scheduler`) ---
    refresh_alerts_minutes: int = Field(default=15, alias="REFRESH_ALERTS_MINUTES")
    refresh_full_at: str = Field(default="03:30", alias="REFRESH_FULL_AT")  # local HH:MM
    timezone: str = Field(default="America/New_York", alias="TIMEZONE")

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"

    def require_api_key(self) -> str:
        if not self.wmata_api_key:
            raise RuntimeError(
                "WMATA_API_KEY is not set. Register for a free key at "
                "https://developer.wmata.com/ and add it to your .env file."
            )
        return self.wmata_api_key

    def require_anthropic_key(self) -> str:
        if not self.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Create one at "
                "https://console.anthropic.com/ and add it to your .env file."
            )
        return self.anthropic_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()
