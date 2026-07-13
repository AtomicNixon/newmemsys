"""
bridge.py — Memory bridge tools: remember_everywhere + resolve_consent bridge handler.

remember_everywhere(content, ...) fans out to both NewMemSys and Vestige in one call.
Failure isolation: Vestige failure never loses the NewMemSys write. A bridge_pending
outbox row is enqueued so the miss is visible and repairable.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Optional

import structlog

from memory_mcp_server import database as db
from memory_mcp_server.tools.memory import remember as _remember_newmemsys
from memory_mcp_server.config import settings

log = structlog.get_logger(__name__)

VESTIGE_EXE      = r"E:\ClaudeAI\vestige.exe"
VESTIGE_DATA_DIR = r"C:/Users/Acat/AppData/Roaming/vestige/core/data"
VESTIGE_INGEST_TIMEOUT = 30   # seconds


# ---------------------------------------------------------------------------
# Vestige CLI helpers
# ---------------------------------------------------------------------------

def _vestige_ingest(content: str) -> str | None:
    """
    Call: vestige.exe ingest --data-dir <dir> "<content>"
    Returns the Vestige node ID string on success, None on failure.
    Never raises — failure is handled by the caller.
    """
    try:
        result = subprocess.run(
            [VESTIGE_EXE, "ingest", "--data-dir", VESTIGE_DATA_DIR, content],
            capture_output=True,
            text=True,
            timeout=VESTIGE_INGEST_TIMEOUT,
        )
        if result.returncode != 0:
            log.warning("vestige_ingest_failed",
                        returncode=result.returncode,
                        stderr=result.stderr[:300])
            return None

        # Parse "Node ID: <uuid>" from stdout
        for line in result.stdout.splitlines():
            m = re.search(r"Node\s+ID:\s*([0-9a-f-]+)", line, re.IGNORECASE)
            if m:
                return m.group(1).strip()

        log.warning("vestige_ingest_no_node_id", stdout=result.stdout[:300])
        return None

    except FileNotFoundError:
        log.error("vestige_exe_not_found", path=VESTIGE_EXE)
        return None
    except subprocess.TimeoutExpired:
        log.error("vestige_ingest_timeout")
        return None
    except Exception as e:
        log.error("vestige_ingest_exception", error=str(e))
        return None


async def _enqueue_bridge_pending(memory_id: str, content: str, reason: str) -> None:
    """Enqueue a bridge_pending outbox row so the missed Vestige write is visible."""
    await db.execute(
        """INSERT INTO outbox (action, payload, ai_reason, status)
           VALUES ('bridge_pending', $1::jsonb, $2, 'pending')""",
        json.dumps({
            "memory_id": memory_id,
            "content_preview": content[:200],
            "direction": "newmemsys→vestige",
        }),
        reason,
    )


async def _store_vestige_node_id(memory_id: str, vestige_node_id: str) -> None:
    await db.execute(
        "UPDATE memories SET vestige_node_id = $1 WHERE id = $2::uuid",
        vestige_node_id, memory_id,
    )


# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------

async def remember_everywhere(
    content: str,
    type: str = "episodic",
    importance: float = 0.5,
    emotional_valence: float = 0.0,
    trust_level: float = 0.8,
    priority: int = 5,
    half_life_hours: int = 720,
    tags: Optional[list] = None,
    context: Optional[dict] = None,
) -> dict:
    """
    Write a memory to BOTH NewMemSys and Vestige in one call.

    NewMemSys write always commits first. If Vestige fails (binary missing,
    DB locked, timeout), the NewMemSys write is preserved and a bridge_pending
    outbox row is enqueued so the miss is visible. A memory is never lost
    because the second system was unavailable.

    Returns: NewMemSys result + vestige_node_id (or bridge_status if Vestige failed).
    """
    tags    = tags or []
    context = context or {}

    # ── Step 1: Write to NewMemSys (must succeed) ─────────────────────────
    result = await _remember_newmemsys(
        content=content,
        type=type,
        importance=importance,
        emotional_valence=emotional_valence,
        trust_level=trust_level,
        priority=priority,
        half_life_hours=half_life_hours,
        tags=tags,
        context=context,
    )
    memory_id = result["id"]
    log.info("remember_everywhere_newmemsys_ok", memory_id=memory_id)

    # ── Step 2: Write to Vestige (failure-isolated) ───────────────────────
    vestige_node_id = _vestige_ingest(content)

    if vestige_node_id:
        await _store_vestige_node_id(memory_id, vestige_node_id)
        log.info("remember_everywhere_vestige_ok",
                 memory_id=memory_id, vestige_node_id=vestige_node_id)
        result["vestige_node_id"] = vestige_node_id
        result["bridge_status"]   = "synced"
    else:
        await _enqueue_bridge_pending(
            memory_id, content,
            "Vestige ingest failed during remember_everywhere — retry via resolve_consent"
        )
        log.warning("remember_everywhere_vestige_failed",
                    memory_id=memory_id, bridge_pending=True)
        result["vestige_node_id"] = None
        result["bridge_status"]   = "bridge_pending — Vestige write failed, outbox queued"

    return result


# ---------------------------------------------------------------------------
# resolve_consent bridge handler
# Called by consent.py resolve_consent when action is bridge_import or bridge_export
# ---------------------------------------------------------------------------

async def handle_bridge_consent(outbox_id: str, action: str, payload: dict) -> dict:
    """
    Execute the actual copy when Bob approves a bridge_import or bridge_export
    consent item.

    bridge_import: Vestige node → NewMemSys memory
    bridge_export: NewMemSys memory → Vestige node
    bridge_pending: retry a previously failed Vestige write
    """
    if action == "bridge_export" or action == "bridge_pending":
        # Copy NewMemSys → Vestige
        memory_id = payload.get("memory_id")
        content   = payload.get("content") or payload.get("content_preview", "")

        if not content and memory_id:
            row = await db.fetchrow(
                "SELECT content, vestige_node_id FROM memories WHERE id = $1::uuid",
                memory_id,
            )
            if row:
                content = row["content"]
                if row["vestige_node_id"]:
                    return {
                        "skipped": True,
                        "reason": "already bridged",
                        "vestige_node_id": row["vestige_node_id"],
                    }

        vestige_node_id = _vestige_ingest(content)
        if vestige_node_id:
            if memory_id:
                await _store_vestige_node_id(memory_id, vestige_node_id)
            return {
                "success": True,
                "action": action,
                "memory_id": memory_id,
                "vestige_node_id": vestige_node_id,
            }
        else:
            return {"success": False, "action": action, "error": "Vestige ingest failed"}

    elif action == "bridge_import":
        # Copy Vestige node → NewMemSys
        vestige_node_id = payload.get("vestige_node_id")
        content         = payload.get("content", "")
        v_type          = payload.get("memory_type", "episodic")
        importance      = float(payload.get("importance", 0.5))
        valence         = float(payload.get("emotional_valence", 0.0))

        if not content:
            return {"success": False, "action": action,
                    "error": "No content in payload — cannot import"}

        result = await _remember_newmemsys(
            content=content,
            type=v_type,
            importance=importance,
            emotional_valence=valence,
            context={"bridged_from": "vestige", "vestige_node_id": vestige_node_id},
            tags=["bridge_import"],
        )
        memory_id = result["id"]
        if vestige_node_id:
            await _store_vestige_node_id(memory_id, vestige_node_id)

        return {
            "success": True,
            "action": action,
            "memory_id": memory_id,
            "vestige_node_id": vestige_node_id,
        }

    return {"success": False, "error": f"Unknown bridge action: {action}"}
