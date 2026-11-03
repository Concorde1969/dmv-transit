"""The MCP server at a web address, for LLMs and apps elsewhere: ``transit-mcp serve --http``.

Speaks MCP's Streamable HTTP transport at ``/mcp``. Anyone may use it without a key
(``MCP_OPEN``), at small limits per network address and a daily cap for all keyless
requests together; that's what AI apps' connector directories need. An API key
(``Authorization: Bearer <key>``, see :mod:`transit_mcp.mcp_keys`) gets its own, larger
limits (``MCP_RATE_PER_MINUTE``, ``MCP_RATE_PER_DAY``). Address lookups here count
against their own Google allowance, never the website's. It runs as its own process,
beside the website rather than inside it: tools run one at a time and would otherwise
hold up the site.
"""

from __future__ import annotations

import json
import logging

from mcp.server.transport_security import TransportSecuritySettings

from transit_mcp.chat.guard import DailyCap, RateLimiter
from transit_mcp.config import get_settings
from transit_mcp.mcp_keys import McpKeys
from transit_mcp.server import app as server_app
from transit_mcp.server.app import mcp

log = logging.getLogger("transit_mcp.mcp_http")

DOCS_URL = "https://dmvtransit.com/mcp-server"


class Gate:
    """ASGI wrapper: who may call, and how often.

    A request with a key needs a valid one and gets that key's limits. Without a key it's
    refused, unless the server is open (``open_limiter`` set): then each network address
    has the open limits, and all keyless requests share ``open_cap`` a day.
    """

    def __init__(self, app, keys: McpKeys, limiter: RateLimiter, *,
                 open_limiter: RateLimiter | None = None, open_cap: DailyCap | None = None):
        self.app = app
        self.keys = keys
        self.limiter = limiter
        self.open_limiter = open_limiter
        self.open_cap = open_cap

    @staticmethod
    def _bearer(scope) -> str | None:
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                kind, _, key = value.decode("latin-1").partition(" ")
                return key.strip() if kind.lower() == "bearer" else ""
        return None

    @staticmethod
    async def _refuse(send, status: int, message: str, headers: dict[str, str]) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                *((k.encode(), v.encode()) for k, v in headers.items())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":             # the server's own startup and shutdown
            await self.app(scope, receive, send)
            return
        token = self._bearer(scope)
        if token is None and self.open_limiter is not None:
            await self._open(scope, receive, send)
            return
        key = self.keys.check(token)
        if key is None:
            await self._refuse(send, 401, "That API key isn't valid." if token else
                               "A valid API key is required: send it as "
                               "'Authorization: Bearer <key>'.",
                               {"www-authenticate": "Bearer"})
            return
        limited = self.limiter.check(str(key["id"]))
        if limited:
            log.info("MCP key %s over its limit (%s)", key["id"], limited)
            wait = 60 if limited == "minute" else 3600
            await self._refuse(send, 429, f"This key is over its limit for the {limited}. "
                               "Please try again later.", {"retry-after": str(wait)})
            return
        await self.app(scope, receive, send)

    async def _open(self, scope, receive, send) -> None:
        """A keyless request. The address only keys the in-memory limiter; it isn't
        logged or stored."""
        address = (scope.get("client") or ("unknown",))[0]
        limited = self.open_limiter.check(address)
        if limited:
            wait = 60 if limited == "minute" else 3600
            await self._refuse(send, 429, f"Too many requests from your network this "
                               f"{limited}. Please try again later, or ask for an API key "
                               f"for higher limits: {DOCS_URL}", {"retry-after": str(wait)})
            return
        if self.open_cap is not None and not self.open_cap.try_take():
            log.warning("keyless MCP requests reached today's cap (%s)", self.open_cap.limit)
            await self._refuse(send, 429, "The server is very busy today. Please try again "
                               "tomorrow.", {"retry-after": "3600"})
            return
        await self.app(scope, receive, send)


def create_app(settings=None, keys: McpKeys | None = None):
    settings = settings or get_settings()
    # One self-contained JSON answer per request, so nothing is kept between calls. The
    # gate decides who gets in (Caddy passes on the public host name), so the SDK's
    # localhost-only check for browsers on this machine doesn't apply.
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False)
    # Callers here get their own Google allowance for address lookups.
    server_app.GEOCODE_BUDGET.update(
        service="google_geocoding_mcp",
        monthly_limit=settings.mcp_google_geocoding_monthly_budget)
    server_app._geocoder.cache_clear()
    is_open = settings.mcp_open
    return Gate(mcp.streamable_http_app(),
                keys or McpKeys.from_settings(settings),
                RateLimiter(settings.mcp_rate_per_minute, settings.mcp_rate_per_day),
                open_limiter=(RateLimiter(settings.mcp_open_rate_per_minute,
                                          settings.mcp_open_rate_per_day) if is_open else None),
                open_cap=(DailyCap(settings.mcp_open_daily_cap, settings.timezone)
                          if is_open else None))


def serve(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(create_app(settings), host=host or settings.mcp_http_host,
                port=port or settings.mcp_http_port,
                log_level="info" if settings.is_production else "warning",
                access_log=False,     # no per-request lines: they'd record addresses
                # Behind Caddy, the caller's own address (for the open limits).
                proxy_headers=settings.trust_proxy,
                forwarded_allow_ips="*" if settings.trust_proxy else None)
