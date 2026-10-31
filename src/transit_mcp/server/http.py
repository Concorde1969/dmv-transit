"""The MCP server at a web address, for LLMs and apps elsewhere: ``transit-mcp serve --http``.

Speaks MCP's Streamable HTTP transport at ``/mcp``. Every request needs an API key
(``Authorization: Bearer <key>``, see :mod:`transit_mcp.mcp_keys`), and each key has its
own limits (``MCP_RATE_PER_MINUTE``, ``MCP_RATE_PER_DAY``). It runs as its own process,
beside the website rather than inside it: tools run one at a time and would otherwise
hold up the site.
"""

from __future__ import annotations

import json
import logging

from mcp.server.transport_security import TransportSecuritySettings

from transit_mcp.chat.guard import RateLimiter
from transit_mcp.config import get_settings
from transit_mcp.mcp_keys import McpKeys
from transit_mcp.server.app import mcp

log = logging.getLogger("transit_mcp.mcp_http")


class RequireKey:
    """ASGI wrapper: refuse any web request without a valid key, or over its limits."""

    def __init__(self, app, keys: McpKeys, limiter: RateLimiter):
        self.app = app
        self.keys = keys
        self.limiter = limiter

    @staticmethod
    def _bearer(scope) -> str | None:
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                kind, _, key = value.decode("latin-1").partition(" ")
                return key.strip() if kind.lower() == "bearer" else None
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
        key = self.keys.check(self._bearer(scope))
        if key is None:
            await self._refuse(send, 401, "A valid API key is required: send it as "
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


def create_app(settings=None, keys: McpKeys | None = None):
    settings = settings or get_settings()
    # One self-contained JSON answer per request, so nothing is kept between calls. Only
    # the key decides who gets in (Caddy passes on the public host name), so the SDK's
    # localhost-only check for browsers on this machine doesn't apply.
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False)
    return RequireKey(mcp.streamable_http_app(),
                      keys or McpKeys.from_settings(settings),
                      RateLimiter(settings.mcp_rate_per_minute, settings.mcp_rate_per_day))


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
                access_log=False)     # no per-request lines: they'd record addresses
