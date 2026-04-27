"""Consent tool: AI can say no. Logs proposed modifications to outbox."""
from __future__ import annotations

import json

from memory_mcp_server import database as db
from memory_mcp_server.tools.memory import _row_to_dict


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
    """Human approves or rejects a pending consent item. decision: 'approved'|'rejected'"""
    if decision not in ("approved", "rejected"):
        return {"error": "decision must be 'approved' or 'rejected'"}
    await db.execute(
        "UPDATE outbox SET status = $1, updated_at = NOW() WHERE id = $2::uuid",
        decision, outbox_id,
    )
    return {"outbox_id": outbox_id, "decision": decision}
