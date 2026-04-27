"""Diary tools: write_diary, read_diary."""
from __future__ import annotations

from typing import Optional

from memory_mcp_server import database as db
from memory_mcp_server.tools.memory import _row_to_dict


async def write_diary(mood: str, entry: str, date: Optional[str] = None) -> dict:
    """Write a diary entry. word count is auto-computed by DB."""
    if date:
        row = await db.fetchrow(
            """INSERT INTO diary (date, mood, entry)
               VALUES ($1::date, $2, $3)
               RETURNING id, date, words, created_at""",
            date, mood, entry,
        )
    else:
        row = await db.fetchrow(
            """INSERT INTO diary (mood, entry)
               VALUES ($1, $2)
               RETURNING id, date, words, created_at""",
            mood, entry,
        )
    return _row_to_dict(row)


async def read_diary(limit: int = 5) -> list[dict]:
    """Return the most recent diary entries."""
    rows = await db.fetch(
        """SELECT id, date, mood, entry, words, created_at
           FROM diary ORDER BY date DESC, created_at DESC LIMIT $1""",
        limit,
    )
    return [_row_to_dict(r) for r in rows]
