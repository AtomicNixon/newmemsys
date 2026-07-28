"""
cli_db.py — shared connection helper for NewMemSys standalone CLIs.

All three CLIs (newmemsys-status, newmemsys-graph, newmemsys-review) import
from here so connection params live in one place. Reads from environment
variables with defaults matching the Docker compose setup.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Optional

import asyncpg

# Defaults match docker-compose.yml for newmemsys_brain
DEFAULT_HOST     = os.environ.get("NEWMEMSYS_DB_HOST", "localhost")
DEFAULT_PORT     = int(os.environ.get("NEWMEMSYS_DB_PORT", "5433"))
DEFAULT_DB       = os.environ.get("NEWMEMSYS_DB_NAME", "memory_system")
DEFAULT_USER     = os.environ.get("NEWMEMSYS_DB_USER", "memory_user")
DEFAULT_PASSWORD = os.environ.get("NEWMEMSYS_DB_PASSWORD", "memsys_secure_2026")


async def connect() -> asyncpg.Pool:
    """Return a small connection pool. Caller is responsible for closing it."""
    try:
        pool = await asyncpg.create_pool(
            host=DEFAULT_HOST,
            port=DEFAULT_PORT,
            database=DEFAULT_DB,
            user=DEFAULT_USER,
            password=DEFAULT_PASSWORD,
            min_size=1,
            max_size=3,
            command_timeout=15,
        )
        return pool
    except Exception as e:
        sys.stderr.write(f"ERROR: cannot connect to NewMemSys DB at "
                         f"{DEFAULT_HOST}:{DEFAULT_PORT}/{DEFAULT_DB}: {e}\n")
        sys.stderr.write("Is the Docker container `newmemsys_brain` running? "
                         "Try: docker start newmemsys_brain\n")
        sys.exit(2)


async def close(pool: asyncpg.Pool) -> None:
    await pool.close()


async def fetch(pool: asyncpg.Pool, sql: str, *args: Any) -> list[asyncpg.Record]:
    return await pool.fetch(sql, *args)


async def fetchrow(pool: asyncpg.Pool, sql: str, *args: Any) -> Optional[asyncpg.Record]:
    return await pool.fetchrow(sql, *args)


async def fetchval(pool: asyncpg.Pool, sql: str, *args: Any) -> Any:
    return await pool.fetchval(sql, *args)
