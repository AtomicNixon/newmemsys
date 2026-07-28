"""Consent tool: AI can say no. Logs proposed modifications to outbox."""
from __future__ import annotations

import json

from memory_mcp_server import database as db
from memory_mcp_server.tools.memory import _row_to_dict


BRIDGE_ACTIONS = {"bridge_import", "bridge_export", "bridge_pending"}
GRAPH_ACTIONS = {"graph_edge_candidate"}


async def consent_check(
    action: str,
    payload: dict,
    ai_reason: str,
) -> dict:
    """
    Log a proposed memory action to the outbox for human review.

    The AI can refuse or flag any memory modification before it executes.
    Status starts as 'pending' — human must approve or reject.

    Returns the outbox record so the caller knows it is queued.
    """
    row = await db.fetchrow(
        """INSERT INTO outbox (action, payload, ai_reason, status)
           VALUES ($1, $2::jsonb, $3, 'pending')
           RETURNING id, action, ai_reason, status, created_at""",
        action, json.dumps(payload), ai_reason,
    )
    result = _row_to_dict(row)
    result["message"] = (
        f"Action '{action}' has been queued for human review. "
        f"AI reason: {ai_reason}"
    )
    return result


async def list_pending_consent() -> list[dict]:
    """Return all pending consent items awaiting human decision."""
    rows = await db.fetch(
        """SELECT id, action, payload, ai_reason, status, created_at
           FROM outbox WHERE status = 'pending' ORDER BY created_at"""
    )
    return [_row_to_dict(r) for r in rows]


async def resolve_consent(outbox_id: str, decision: str) -> dict:
    """Human approves or rejects a pending consent item. decision: 'approved'|'rejected'

    For bridge_import / bridge_export / bridge_pending actions, 'approved'
    immediately executes the cross-system copy via the bridge handler.
    For graph_edge_candidate actions, 'approved' inserts the edge into
    memory_graph.
    """
    if decision not in ("approved", "rejected"):
        return {"error": "decision must be 'approved' or 'rejected'"}

    # Fetch the outbox row to check action type
    row = await db.fetchrow(
        "SELECT action, payload FROM outbox WHERE id = $1::uuid",
        outbox_id,
    )
    if not row:
        return {"error": f"outbox item not found: {outbox_id}"}

    action  = row["action"]
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")

    # Mark resolved first (idempotent even if execution fails)
    await db.execute(
        "UPDATE outbox SET status = $1, updated_at = NOW() WHERE id = $2::uuid",
        decision, outbox_id,
    )
    result = {"outbox_id": outbox_id, "action": action, "decision": decision}

    # Execute bridge actions when approved
    if decision == "approved" and action in BRIDGE_ACTIONS:
        try:
            from memory_mcp_server.tools.bridge import handle_bridge_consent
            bridge_result = await handle_bridge_consent(outbox_id, action, payload)
            result["bridge"] = bridge_result
        except Exception as e:
            result["bridge"] = {"error": str(e), "note": "outbox marked approved; bridge execution failed"}

    # Execute graph edge actions when approved
    if decision == "approved" and action in GRAPH_ACTIONS:
        try:
            memory_id    = payload.get("memory_id")
            candidate_id = payload.get("candidate_id")
            similarity   = payload.get("similarity", 0.8)
            rel_type     = payload.get("proposed_relationship", "related_to")

            if not memory_id or not candidate_id:
                result["graph"] = {"error": "missing memory_id or candidate_id in payload"}
            else:
                await db.execute(
                    """INSERT INTO memory_graph
                       (memory_id, connected_memory_id, relationship_type, confidence, context)
                       VALUES ($1::uuid, $2::uuid, $3::relationship_type, $4, $5)""",
                    memory_id, candidate_id, rel_type, float(similarity),
                    "proposed by dream consolidation",
                )
                result["graph"] = {
                    "success": True,
                    "memory_id": memory_id,
                    "connected_memory_id": candidate_id,
                    "relationship_type": rel_type,
                    "confidence": similarity,
                }
        except Exception as e:
            result["graph"] = {"error": str(e), "note": "outbox marked approved; edge creation failed"}

    return result
