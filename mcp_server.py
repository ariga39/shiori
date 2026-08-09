#!/usr/bin/env python3
"""MCP stdio server exposing session-memory query.search as a read-only tool.

Exposes a single tool `search(query, limit=5)` that runs the hybrid retrieval
in query.search() and returns structured results. This layer is strictly
read-only: no ingest/write tools are registered.

Run with:  ./venv/bin/python mcp_server.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ContentBlock

import query
from shiyi.config import Settings, load_config

MAX_LIMIT = 20
DEFAULT_LIMIT = 5

TOOL_DESCRIPTION = (
    "Search session memory (hybrid vector + BM25 retrieval). "
    "Returns up to `limit` (default 5, max 20) results, each with "
    "content, score, timestamp, session_id, and source_type. Read-only."
)


def _serialize_ts(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def run_search(query_text, limit=DEFAULT_LIMIT):
    """Run a search, returning a JSON-serializable dict.

    On any error (empty query, embedding failure, DB unreachable) returns
    a stable, secret-safe error object. Exception text is deliberately omitted
    because database/client errors can contain DSNs or credentials.
    """
    if not query_text or not query_text.strip():
        return {"error": "query must be a non-empty string"}

    clamped = max(1, min(int(limit), MAX_LIMIT))

    try:
        rows = query.search(query_text, limit=clamped)
    except Exception as exc:  # noqa: BLE001 - map failures to a safe public result
        return {"error": {"code": "search_failed", "type": type(exc).__name__}}

    results = [
        {
            "content": row[0],
            "score": row[1],
            "timestamp": _serialize_ts(row[2]),
            "session_id": row[3],
            "source_type": row[4],
        }
        for row in rows
    ]
    return {"results": results, "count": len(results)}


async def _search_tool(query: str, limit: int = DEFAULT_LIMIT) -> dict:
    return run_search(query, limit)


class ShiyiMCPServer(FastMCP):
    """FastMCP server with the pre-1.0 call result compatibility shape."""

    async def call_tool(self, name: str, arguments: dict) -> CallToolResult:
        result = await super().call_tool(name, arguments)
        if isinstance(result, CallToolResult):
            return result
        return CallToolResult(content=cast(list[ContentBlock], list(result)))


def build_server() -> ShiyiMCPServer:
    server = ShiyiMCPServer(
        name="session-memory",
        instructions=(
            "Provides the `search` tool for retrieving relevant session memory "
            "chunks. Read-only: no write or ingest tools are exposed."
        ),
    )
    server.tool(name="search", description=TOOL_DESCRIPTION)(_search_tool)
    return server


async def _amain():
    server = build_server()
    await server.run_stdio_async()


async def run_server(settings: Settings | None = None):
    """Run the configured read-only server for the installed CLI."""
    if settings is not None:
        query.apply_settings(settings)
    await _amain()


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="shiyi read-only MCP server")
    parser.add_argument("--config", help="JSON/TOML config file")
    parser.add_argument("--legacy-openclaw", action="store_true")
    args = parser.parse_args(argv)
    settings = load_config(config_path=args.config, legacy_openclaw=args.legacy_openclaw)
    settings.require_database()
    settings.require_embedding()
    query.apply_settings(settings)

    asyncio.run(run_server(settings))


if __name__ == "__main__":
    main()
