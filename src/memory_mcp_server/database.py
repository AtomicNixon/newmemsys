"""asyncpg connection pool — singleton."""
from __future__ import annotations

import asyncpg
import structlog

from memory_mcp_server.config import settings

log = structlog.get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        log.info("Creating DB pool", dsn=settings.dsn.replace(settings.postgres_password, "***"))
        _pool = await asyncpg.create_pool(
            dsn=settings.dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def execute(sql: str, *args) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(sql, *args)


async def fetch(sql: str, *args) -> list[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(sql, *args)


async def fetchrow(sql: str, *args) -> asyncpg.Record | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)


async def fetchval(sql: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, *args)
