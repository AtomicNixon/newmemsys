"""Shared MCP stdio client for NewMemSys hook scripts.

Spawns the memory_mcp_server Python module via stdio and exposes a simple
async `call_tool(name, **args)` interface. Hook scripts should use this
instead of talking directly to Postgres, so all storage/scoring logic stays
behind the MCP server.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


# Try to find the Python interpreter configured for the MCP server.
# Prefer the same one that runs Claude Code's memory server if known.
DEFAULT_PYTHON = os.environ.get("NEWMEMSYS_PYTHON", "C:/Python312/python.exe")


def _server_params() -> StdioServerParameters:
    src_dir = Path(__file__).resolve().parent.parent.parent / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src_dir)
    return StdioServerParameters(
        command=DEFAULT_PYTHON,
        args=["-m", "memory_mcp_server"],
        env=env,
    )


@asynccontextmanager
async def mcp_session():
    """Async context manager yielding a connected ClientSession."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool(session: ClientSession, name: str, **args: Any) -> Any:
    """Call an MCP tool by name and return its parsed content."""
    result = await session.call_tool(name, arguments=args)
    if result.isError:
        texts = [c.text for c in result.content if hasattr(c, "text")]
        raise RuntimeError(f"Tool {name} error: {''.join(texts)}")
    texts = [c.text for c in result.content if hasattr(c, "text")]
    payload = "".join(texts)
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload


async def call_tools_in_order(calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """Convenience helper for scripts that need a fixed sequence of calls."""
    results = []
    async with mcp_session() as session:
        for name, args in calls:
            results.append(await call_tool(session, name, **args))
    return results


# Synchronous convenience wrapper for hook scripts that run synchronously.
def run(coro):
    return asyncio.run(coro)
