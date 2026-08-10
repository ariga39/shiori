#!/usr/bin/env python3
"""MCP stdio server exposing session-memory query.search as a read-only tool.

Exposes a single tool `search(query, limit=5, offset=0)` that runs the hybrid
retrieval in query.search() and returns bounded, provenance-bearing results.
This layer is strictly read-only: no ingest/write tools are registered.

Run with:  ./venv/bin/python mcp_server.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import cast

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ContentBlock

import query
from shiori.config import ConfigError, Settings, load_config

MAX_LIMIT = 20
DEFAULT_LIMIT = 5

TOOL_DESCRIPTION = (
    "Search session memory (hybrid vector + BM25 retrieval). "
    "Returns a bounded page (limit default 5, max 20; offset is bounded) with "
    "has_more/next_offset and content, score, timestamp, session_id, "
    "source_type, model/dimension, and provenance. Incompatible embedding "
    "models/dimensions are excluded. Read-only; no writes are exposed."
)


def _serialize_ts(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _public_error(exc: Exception) -> dict[str, str]:
    """Map failures to a stable, non-sensitive MCP response."""
    if isinstance(exc, (query.QueryError, ConfigError)):
        return {"code": exc.code}
    return {"code": "search_failed", "type": type(exc).__name__}


def _invalid_input(code: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code}}


def run_search(query_text, limit=DEFAULT_LIMIT, offset=0):
    """Run a search, returning a JSON-serializable dict.

    On any error (empty query, embedding failure, DB unreachable) returns
    a stable, secret-safe error object. Exception text is deliberately omitted
    because database/client errors can contain DSNs or credentials.
    """
    if not isinstance(query_text, str):
        return _invalid_input("invalid_query")
    if not query_text.strip():
        return _invalid_input("invalid_query")
    try:
        query_text = query._validate_query_text(query_text)
    except query.QueryError as exc:
        return {"error": {"code": exc.code}}
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return _invalid_input("invalid_limit")
    if isinstance(offset, bool) or not isinstance(offset, int):
        return _invalid_input("invalid_offset")
    if offset < 0 or offset > query.MAX_OFFSET:
        return _invalid_input("offset_out_of_bounds")

    # Clamp oversized pages at the MCP boundary.  This keeps compatibility
    # with existing callers while guaranteeing a bounded provider/DB request.
    clamped = min(limit, MAX_LIMIT)

    try:
        page = query.search_page(query_text, limit=clamped, offset=offset)
        results = [
            _serialize_result(row)
            for row in page.results
        ]
    except Exception as exc:  # noqa: BLE001 - map failures to a safe public result
        return {"error": _public_error(exc)}

    return {
        "results": results,
        "count": len(results),
        "limit": page.limit,
        "offset": page.offset,
        "has_more": page.has_more,
        "next_offset": page.next_offset,
    }


def _serialize_result(row: tuple) -> dict:
    provenance = query._row_provenance(row)
    provenance["timestamp"] = _serialize_ts(provenance["timestamp"])
    return {
        "content": row[0],
        "score": row[1],
        "timestamp": provenance["timestamp"],
        "session_id": provenance["session_id"],
        "source_type": provenance["source_type"],
        "embedding_model": provenance["embedding_model"],
        "embedding_dimension": provenance["embedding_dimension"],
        "provenance": provenance,
    }


async def _search_tool(query: str, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict:
    return run_search(query, limit, offset)


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

    parser = argparse.ArgumentParser(description="shiori read-only MCP server")
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
