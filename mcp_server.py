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

import query
from mcp.server import MCPServer

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
    {"error": "..."} with a readable message — never a raw stack trace.
    """
    if not query_text or not query_text.strip():
        return {"error": "query must be a non-empty string"}

    clamped = max(1, min(int(limit), MAX_LIMIT))

    try:
        rows = query.search(query_text, limit=clamped)
    except Exception as exc:  # noqa: BLE001 - surface a readable message
        return {"error": f"search failed: {type(exc).__name__}: {exc}"}

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


def build_server() -> MCPServer:
    server = MCPServer(
        name="session-memory",
        title="session-memory-pg",
        description="Read-only MCP server for session memory search.",
        instructions=(
            "Provides the `search` tool for retrieving relevant session memory "
            "chunks. Read-only: no write or ingest tools are exposed."
        ),
    )
    server.add_tool(
        _search_tool,
        name="search",
        description=TOOL_DESCRIPTION,
    )
    return server


async def _amain():
    server = build_server()
    await server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(_amain())
