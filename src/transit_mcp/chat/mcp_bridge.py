"""Keep a live MCP ClientSession to the transit server and adapt it for Claude.

The transit MCP server is launched as a subprocess over stdio (same interpreter
and environment as this process), so the chat app needs nothing running
separately beyond ``transit-mcp ingest`` having been done at least once.
"""

from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpBridge:
    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[dict[str, Any]] = []

    async def start(self) -> None:
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "transit_mcp.server.app"],
            env=env,
        )
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

        listed = await self._session.list_tools()
        self._tools = [
            {
                "name": t.name,
                "description": (t.description or "").strip(),
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
            }
            for t in listed.tools
        ]

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    @property
    def anthropic_tools(self) -> list[dict[str, Any]]:
        return self._tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Invoke an MCP tool. Returns ``(text, is_error)``."""
        if self._session is None:
            return ("MCP session is not running.", True)
        try:
            result = await self._session.call_tool(name, arguments or {})
        except Exception as exc:  # noqa: BLE001
            return (f"Tool call failed: {exc}", True)

        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(str(block))
        return ("\n".join(parts).strip() or "(no content)", bool(result.isError))
