#!/usr/bin/env python3
"""Exercise the installed CLI's real read-only MCP stdio boundary."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _run(cli: Path, config: Path) -> None:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    server = StdioServerParameters(
        command=str(cli),
        args=["--config", str(config), "serve"],
        env=env,
        cwd=str(config.parent),
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            if names != ["search"]:
                raise RuntimeError(f"unexpected MCP tools: {names!r}")
            result = await session.call_tool(
                "search",
                {"query": "synthetic clean-machine smoke", "limit": 2, "offset": 0},
            )
            if result.isError:
                raise RuntimeError("MCP search returned a protocol error")
            if not result.structuredContent and not result.content:
                raise RuntimeError("MCP search returned no result payload")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(_run(args.cli, args.config))
    print("mcp stdio smoke ok: search-only tool surface")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
