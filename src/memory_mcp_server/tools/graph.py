"""Graph tools: connect, find_causes, find_contradictions (human-curated only)."""
from __future__ import annotations

import uuid
from typing import Optional

from memory_mcp_server import database as db
from memory_mcp_server.tools.memory import _row_to_dict


async def connect(
    from_id: str,
    to_id: str,
    relationship_type: str = "related_to",
    confidence: float = 0.8,
    context: Optional[str] = None,
) -> dict:
    """Create a human-curated edge between two memories."""
    row = await db.fetchrow(
        """INSERT INTO memory_graph
             (memory_id, connected_memory_id, relationship_type, confidence, context)
           VALUES ($1::uuid, $2::uuid, $3::relationship_type, $4, $5)
           RETURNING id, created_at""",
        from_id, to_id, relationship_type, confidence, context,
    )
    return {"edge_id": str(row["id"]), "created_at": str(row["created_at"])}


async def find_causes(memory_id: str, depth: int = 3) -> list[dict]:
    """Recursive causal chain from a memory."""
    rows = await db.fetch(
        "SELECT * FROM find_causes($1::uuid, $2)",
        memory_id, depth,
    )
    return [_row_to_dict(r) for r in rows]


async def find_contradictions(memory_id: str) -> list[dict]:
    """Find memories that contradict this one."""
    rows = await db.fetch(
        "SELECT * FROM find_contradictions($1::uuid)",
        memory_id,
    )
    return [_row_to_dict(r) for r in rows]


async def connect_batch(edges: list[dict]) -> list[dict]:
    """Bulk-create graph edges."""
    results = []
    for edge in edges:
        result = await connect(**edge)
        results.append(result)
    return results
