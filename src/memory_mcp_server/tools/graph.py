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


async def disconnect_batch(edges: list[dict]) -> list[dict]:
    """Bulk-delete graph edges from memory_graph.

    Each edge dict may contain an ``id`` key (preferred, safest) or the
    triple ``from_id`` + ``to_id`` + ``relationship_type``. If ``id`` is
    provided, the exact edge row is deleted. Otherwise all rows matching the
    triple are removed.

    Note: This only touches the relational memory_graph table. Mirroring the
    delete into the Apache AGE cognitive graph is a known separate gap.
    """
    results = []
    for edge in edges:
        result: dict = {
            "success": False,
            "edge_id": None,
            "deleted": False,
            "reason": None,
        }
        edge_id = edge.get("id") or edge.get("edge_id")
        try:
            if edge_id:
                row = await db.fetchrow(
                    "DELETE FROM memory_graph WHERE id = $1::uuid RETURNING id",
                    edge_id,
                )
                if row:
                    result.update({
                        "success": True,
                        "edge_id": str(row["id"]),
                        "deleted": True,
                    })
                else:
                    result["reason"] = "not found"
            else:
                from_id = edge.get("from_id")
                to_id = edge.get("to_id")
                rel = edge.get("relationship_type", "related_to")
                if not from_id or not to_id:
                    result["reason"] = "missing from_id/to_id or id"
                    results.append(result)
                    continue

                row = await db.fetchrow(
                    """DELETE FROM memory_graph
                       WHERE memory_id = $1::uuid
                         AND connected_memory_id = $2::uuid
                         AND relationship_type = $3::relationship_type
                       RETURNING id""",
                    from_id, to_id, rel,
                )
                if row:
                    result.update({
                        "success": True,
                        "edge_id": str(row["id"]),
                        "deleted": True,
                    })
                else:
                    result["reason"] = "not found"
        except Exception as exc:
            result["reason"] = f"error: {exc}"
        results.append(result)
    return results
