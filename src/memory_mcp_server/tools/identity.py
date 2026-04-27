"""Identity tools: get_identity, get_worldview, set_worldview, get_drives, get_goals."""
from __future__ import annotations

from typing import Optional

from memory_mcp_server import database as db
from memory_mcp_server.tools.memory import _row_to_dict


async def get_identity() -> list[dict]:
    """Return all identity keys ordered by priority (highest first)."""
    rows = await db.fetch(
        "SELECT key, value, priority, modified_at FROM identity ORDER BY priority DESC"
    )
    return [_row_to_dict(r) for r in rows]


async def get_worldview() -> list[dict]:
    """Return all worldview beliefs ordered by confidence."""
    rows = await db.fetch(
        "SELECT id, topic, belief, confidence, source, contradicted_by FROM worldview ORDER BY confidence DESC"
    )
    return [_row_to_dict(r) for r in rows]


async def set_worldview(
    topic: str,
    belief: str,
    confidence: float = 0.7,
    source: Optional[str] = None,
    contradicts_id: Optional[str] = None,
) -> dict:
    """
    Upsert a worldview belief. If a belief with the same topic already exists,
    update it in place. If contradicts_id is supplied, append it to that
    belief's contradicted_by array and link back symmetrically.
    """
    import json

    confidence = max(0.0, min(1.0, confidence))

    # Upsert on topic
    row = await db.fetchrow(
        """INSERT INTO worldview (topic, belief, confidence, source)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (topic) DO UPDATE
             SET belief     = EXCLUDED.belief,
                 confidence = EXCLUDED.confidence,
                 source     = COALESCE(EXCLUDED.source, worldview.source)
           RETURNING id, topic, belief, confidence, source, contradicted_by""",
        topic, belief, confidence, source,
    )
    result = _row_to_dict(row)
    new_id = str(row["id"])

    # Wire contradiction links if requested
    if contradicts_id:
        # Mark the new belief as contradicted_by contradicts_id
        await db.execute(
            """UPDATE worldview
               SET contradicted_by = array_append(
                   COALESCE(contradicted_by, '{}'), $1::uuid)
               WHERE id = $2::uuid
                 AND NOT ($1::uuid = ANY(COALESCE(contradicted_by, '{}')))""",
            contradicts_id, new_id,
        )
        # Mark the target belief as contradicted_by new_id (symmetric)
        await db.execute(
            """UPDATE worldview
               SET contradicted_by = array_append(
                   COALESCE(contradicted_by, '{}'), $1::uuid)
               WHERE id = $2::uuid
                 AND NOT ($1::uuid = ANY(COALESCE(contradicted_by, '{}')))""",
            new_id, contradicts_id,
        )
        result["contradicts_id"] = contradicts_id

    return result


async def get_drives() -> list[dict]:
    """Return currently active (non-expired) drives."""
    rows = await db.fetch(
        "SELECT id, concept, level, source, ttl_hours, created_at, expires_at FROM v_active_drives"
    )
    return [_row_to_dict(r) for r in rows]


async def get_goals() -> list[dict]:
    """Return currently active goals."""
    rows = await db.fetch(
        "SELECT id, title, description, priority, source, deadline, created_at FROM v_active_goals"
    )
    return [_row_to_dict(r) for r in rows]


async def set_identity(key: str, value: dict, priority: int = 5) -> dict:
    """Upsert an identity key."""
    import json
    await db.execute(
        """INSERT INTO identity (key, value, priority, modified_at)
           VALUES ($1, $2::jsonb, $3, NOW())
           ON CONFLICT (key) DO UPDATE
             SET value = EXCLUDED.value,
                 priority = EXCLUDED.priority,
                 modified_at = NOW()""",
        key, json.dumps(value), priority,
    )
    return {"key": key, "updated": True}
