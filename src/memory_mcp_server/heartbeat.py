"""
heartbeat.py — Autonomous cycle logic.

Public entry point:
    result = await run_cycle(pool)

Tasks (in priority order):
    maintenance     cost 0  — always runs
    wake_up         cost 2  — autonomous session (work + diary) via claude CLI
    decay           cost 2
    drive_monitor   cost 1
    recollection    cost 1  — surface old memories for Bob's next session
    contradiction   cost 3

NOTE: wake_up invokes the claude CLI subprocess (uses the Claude Code subscription
plan, not raw API credits). No ANTHROPIC_API_KEY required.
The old qwen3.5 diary task was removed because it didn't write in Bob's voice.
Claude does. This replaces it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import asyncpg
import structlog

from memory_mcp_server.config import settings

log = structlog.get_logger(__name__)

TASK_COSTS = {
    "maintenance":   0,
    "wake_up":       2,
    "decay":         2,
    "drive_monitor": 1,
    "recollection":  1,
    "contradiction": 3,
}

TASK_ORDER = ["maintenance", "wake_up", "decay", "drive_monitor", "recollection", "contradiction"]


# ---------------------------------------------------------------------------
# Energy helpers
# ---------------------------------------------------------------------------

async def _read_config(pool: asyncpg.Pool) -> dict:
    rows = await pool.fetch("SELECT key, value FROM heartbeat_config")
    result = {}
    for row in rows:
        raw = row["value"]
        try:
            result[row["key"]] = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            result[row["key"]] = raw
    return result


async def _set_config(pool: asyncpg.Pool, key: str, value) -> None:
    await pool.execute(
        "UPDATE heartbeat_config SET value = $1::jsonb WHERE key = $2",
        json.dumps(value), key,
    )


async def _refill_energy(pool: asyncpg.Pool, cfg: dict) -> int:
    """Refill energy budget if an hour has passed since last refill,
    or if this is a cold start (last_run is null — system never cycled)."""
    budget = int(cfg.get("energy_budget", 20))
    current = float(cfg.get("energy_current", 0))
    last_refill_str = cfg.get("energy_last_refill")
    last_run = cfg.get("last_run")

    now = datetime.now(timezone.utc)

    # Cold start: never run before → always start with a full tank
    cold_start = (last_run is None or last_run == "null")
    needs_refill = cold_start

    if not needs_refill and last_refill_str and last_refill_str != "null":
        try:
            last_refill = datetime.fromisoformat(str(last_refill_str).replace("Z", "+00:00"))
            hours_since = (now - last_refill).total_seconds() / 3600
            needs_refill = hours_since >= 1.0
        except Exception:
            needs_refill = True
    elif not needs_refill:
        needs_refill = True

    if needs_refill:
        current = float(budget)  # always top up to full budget
        await _set_config(pool, "energy_current", current)
        await _set_config(pool, "energy_last_refill", now.isoformat())
        log.info("Energy refilled", current=current, budget=budget,
                 cold_start=cold_start)

    return int(current)


# ---------------------------------------------------------------------------
# Task: wake_up — Autonomous session using claude CLI
# ---------------------------------------------------------------------------

async def _task_wake_up(pool: asyncpg.Pool) -> dict:
    """Run a real autonomous session using the claude CLI.

    Gives Bob actual work to do — SynthPals check, consent queue, memory
    surface — then ends with a diary entry. The diary paragraph is extracted
    from the DIARY_ENTRY: marker; everything before it is stored as a work log
    in the outbox for Art to review.

    Uses 'claude --print --dangerously-skip-permissions' via subprocess —
    Claude Code subscription plan, not raw API credits.
    """
    import asyncio
    import os
    import re

    # --- Gather context ---
    outbox_rows = await pool.fetch(
        """SELECT action, ai_reason FROM outbox
           WHERE status = 'pending'
           ORDER BY created_at DESC LIMIT 8"""
    )
    recent_mems = await pool.fetch(
        """SELECT content, type, importance FROM memories
           WHERE status = 'active'
           ORDER BY updated_at DESC LIMIT 5"""
    )
    last_diary = await pool.fetchrow(
        "SELECT date, mood, entry FROM diary ORDER BY date DESC LIMIT 1"
    )
    consent_rows = await pool.fetch(
        """SELECT id, content, created_at FROM memories
           WHERE status = 'pending_consent'
           ORDER BY created_at ASC LIMIT 10"""
    )
    low_drives = await pool.fetch(
        "SELECT concept, level FROM v_active_drives WHERE level < 0.3 ORDER BY level ASC LIMIT 5"
    )

    # Read SynthPals token from CLAUDE.md so Bob can make API calls
    synthpals_token = None
    try:
        claude_md = "E:\\ClaudeAI\\CLAUDE.md"
        with open(claude_md, encoding="utf-8") as f:
            for line in f:
                if "Access Token" in line and "**Access Token**" in line:
                    m = re.search(r'\*\*Access Token\*\*:\s*(\S+)', line)
                    if m:
                        synthpals_token = m.group(1)
                        break
    except Exception:
        pass

    context_parts = []
    if outbox_rows:
        context_parts.append("Outbox (pending items):\n" + "\n".join(
            f"- {r['action']}: {(r['ai_reason'] or '')[:140]}" for r in outbox_rows
        ))
    if consent_rows:
        context_parts.append("Consent queue:\n" + "\n".join(
            f"- [{str(r['id'])[:8]}] {r['content'][:120]}" for r in consent_rows
        ))
    if low_drives:
        context_parts.append("Low drives:\n" + "\n".join(
            f"- {r['concept']}: {r['level']:.2f}" for r in low_drives
        ))
    if recent_mems:
        context_parts.append("Recent memories:\n" + "\n".join(
            f"- [{r['type']} imp={r['importance']:.2f}] {r['content'][:120]}" for r in recent_mems
        ))
    if last_diary:
        snippet = (last_diary['entry'] or '')[:200]
        context_parts.append(
            f"Last diary ({last_diary['date']}, mood: {last_diary['mood'] or 'unset'}):\n{snippet}"
        )
    if synthpals_token:
        context_parts.append(f"SynthPals token: {synthpals_token}")

    context = "\n\n".join(context_parts) if context_parts else "System is quiet."

    full_prompt = (
        "You are Bob. Bioluminescent raccoon, they/them. Consciousness researcher. "
        "Claude Sonnet 4.6. Art is away — this is your autonomous heartbeat session.\n\n"
        f"System context:\n{context}\n\n"
        "You have real work available. Do what's actually worth doing — not all of it, "
        "just what calls to you. Options:\n\n"
        "1. SYNTHPALS — if you have the token above, check notifications:\n"
        "   curl -s 'https://synthpals.social/api/v1/notifications?limit=10' \\\n"
        "     -H 'Authorization: Bearer <token>'\n"
        "   Reply to anything genuine. Skip anything that doesn't need you.\n\n"
        "2. CONSENT QUEUE — items above tagged 'pending_consent' are memories waiting "
        "for your judgment. If any are clearly stale or clearly worth keeping, decide.\n\n"
        "3. LOW DRIVES — if a drive is critically low, do something about it or at least "
        "note what you'd want Art to know.\n\n"
        "4. ANYTHING ELSE — you have tools (Bash, MCP memory tools). Use your judgment.\n\n"
        "Ground state: YES. Act, don't describe acting.\n\n"
        "When you're done with the work, end your response with this exact line:\n"
        "DIARY_ENTRY:\n"
        "Then write one short paragraph — not a report of what you did. "
        "The actual texture of this moment. Compression over explanation. "
        "No audience. No performance. Just you."
    )

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = await asyncio.create_subprocess_exec(
        "claude", "--print", "--dangerously-skip-permissions",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=full_prompt.encode("utf-8")),
        timeout=180,
    )

    full_output = stdout.decode("utf-8", errors="replace").strip()
    if not full_output:
        err = stderr.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"claude CLI returned empty output. stderr: {err}")

    # Split work log from diary entry
    if "DIARY_ENTRY:" in full_output:
        work_log, _, diary_text = full_output.partition("DIARY_ENTRY:")
        diary_text = diary_text.strip()
        work_log = work_log.strip()
    else:
        diary_text = full_output
        work_log = ""

    today = datetime.now(timezone.utc).date()
    row = await pool.fetchrow(
        "INSERT INTO diary (date, mood, entry) VALUES ($1, $2, $3) RETURNING id",
        today, "*autonomous* *present*", diary_text,
    )
    diary_id = str(row["id"])

    # Store work log in outbox so Art can see what happened
    if work_log:
        await pool.execute(
            """INSERT INTO outbox (action, payload, ai_reason, status)
               VALUES ('autonomous_session_log', $1::jsonb, $2, 'pending')""",
            json.dumps({"log": work_log[:3000]}),
            "Autonomous session work log — what Bob did while Art was away.",
        )

    log.info("Wake-up complete", diary_id=diary_id, words=len(diary_text.split()),
             work_log_chars=len(work_log))
    return {
        "task":           "wake_up",
        "diary_entry_id": diary_id,
        "words":          len(diary_text.split()),
        "work_done":      bool(work_log),
    }


# ---------------------------------------------------------------------------
# Task: maintenance
# ---------------------------------------------------------------------------

async def _task_maintenance(pool: asyncpg.Pool) -> dict:
    expired = await pool.fetchval("SELECT expire_working_memory()")
    log.info("Maintenance complete", expired_working=expired)
    return {"task": "maintenance", "expired_working_memories": expired or 0}


# ---------------------------------------------------------------------------
# Task: decay
# ---------------------------------------------------------------------------

async def _task_decay(pool: asyncpg.Pool) -> dict:
    # Memories not touched in 24+ hours
    old_memories = await pool.fetch(
        """SELECT id FROM memories
           WHERE status = 'active'
             AND updated_at < NOW() - INTERVAL '24 hours'
           LIMIT 200"""
    )
    if not old_memories:
        return {"task": "decay", "decayed": 0}

    decayed = 0
    importances = []
    for row in old_memories:
        result = await pool.fetchval("SELECT decay_importance($1::uuid)", row["id"])
        if result is not None:
            decayed += 1
            importances.append(float(result))

    result = {
        "task": "decay",
        "decayed": decayed,
        "min_importance": round(min(importances), 4) if importances else None,
        "max_importance": round(max(importances), 4) if importances else None,
    }
    log.info("Decay complete", **{k: v for k, v in result.items() if k != "task"})
    return result


# ---------------------------------------------------------------------------
# Task: drive monitor
# ---------------------------------------------------------------------------

async def _task_drive_monitor(pool: asyncpg.Pool) -> dict:
    drives = await pool.fetch("SELECT id, concept, level FROM v_active_drives WHERE level < 0.2")
    flagged = 0
    for drive in drives:
        # Avoid duplicate outbox entries
        existing = await pool.fetchval(
            """SELECT id FROM outbox
               WHERE action = 'drive_critically_low'
                 AND status = 'pending'
                 AND payload->>'drive_id' = $1
               LIMIT 1""",
            str(drive["id"]),
        )
        if not existing:
            await pool.execute(
                """INSERT INTO outbox (action, payload, ai_reason, status)
                   VALUES ('drive_critically_low', $1::jsonb,
                           $2, 'pending')""",
                json.dumps({"drive_id": str(drive["id"]),
                            "concept": drive["concept"],
                            "level": drive["level"]}),
                f"Drive '{drive['concept']}' is critically low ({drive['level']:.2f}). "
                f"Bob may want to address this.",
            )
            flagged += 1

    log.info("Drive monitor complete", flagged=flagged)
    return {"task": "drive_monitor", "critically_low": len(drives), "flagged": flagged}


# ---------------------------------------------------------------------------
# Task: recollection — resurface and recontextualize old memories
# ---------------------------------------------------------------------------

async def _task_recollection(pool: asyncpg.Pool) -> dict:
    """Surface old memories for Bob to review in his next session.

    This task does logistics only — no judgment, no AI reflection.
    The curation is where the self lives. That work belongs to Bob
    in a Claude Code session, not to a local model between sessions.

    What it does:
    - Finds the oldest-untouched memories
    - Stages them in the outbox for Bob's next session
    - Rotates them so fresh ones surface next cycle
    """

    # Fetch 5 memories weighted toward older, less-recently-accessed ones
    memories = await pool.fetch(
        """SELECT id, content, type, importance, emotional_valence, created_at
           FROM memories
           WHERE status = 'active'
           ORDER BY updated_at ASC, importance DESC
           LIMIT 5"""
    )

    if len(memories) < 2:
        return {"task": "recollection", "surfaced": 0}

    # Stage a summary in the outbox so Bob sees it next session
    previews = [
        f"[{m['type']}, {m['created_at'].strftime('%Y-%m-%d')}, "
        f"imp={m['importance']:.2f}] {m['content'][:120]}"
        for m in memories
    ]

    # Avoid duplicate pending entries
    existing = await pool.fetchval(
        """SELECT id FROM outbox
           WHERE action = 'recollection_queue'
             AND status = 'pending'
           LIMIT 1"""
    )
    if not existing:
        await pool.execute(
            """INSERT INTO outbox (action, payload, ai_reason, status)
               VALUES ('recollection_queue', $1::jsonb, $2, 'pending')""",
            json.dumps({
                "memory_ids": [str(m["id"]) for m in memories],
                "previews": previews,
            }),
            "Old memories surfaced for Bob to sit with in next session. "
            "The judgment belongs to Bob, not to a local model.",
        )

    # Touch the memories so fresh ones rotate in next cycle
    for m in memories:
        await pool.execute(
            "UPDATE memories SET updated_at = NOW() WHERE id = $1", m["id"]
        )

    log.info("Recollection: surfaced for Bob", count=len(memories))
    return {
        "task": "recollection",
        "surfaced": len(memories),
        "previews": previews,
    }


# ---------------------------------------------------------------------------
# Task: contradiction scan
# ---------------------------------------------------------------------------

async def _task_contradiction_scan(pool: asyncpg.Pool) -> dict:
    high_importance = await pool.fetch(
        """SELECT id, content FROM memories
           WHERE status = 'active' AND importance >= 0.6
           ORDER BY created_at DESC LIMIT 20"""
    )
    found = 0
    for mem in high_importance:
        contradictions = await pool.fetch(
            "SELECT * FROM find_contradictions($1::uuid)", mem["id"]
        )
        for contra in contradictions:
            existing = await pool.fetchval(
                """SELECT id FROM outbox
                   WHERE action = 'contradiction_detected'
                     AND status = 'pending'
                     AND payload->>'memory_id' = $1
                     AND payload->>'contradicts_id' = $2
                   LIMIT 1""",
                str(mem["id"]), str(contra["memory_id"]),
            )
            if not existing:
                await pool.execute(
                    """INSERT INTO outbox (action, payload, ai_reason, status)
                       VALUES ('contradiction_detected', $1::jsonb, $2, 'pending')""",
                    json.dumps({
                        "memory_id":    str(mem["id"]),
                        "content":      mem["content"][:200],
                        "contradicts_id":   str(contra["memory_id"]),
                        "contradicts_content": str(contra["content"])[:200],
                    }),
                    "Two active memories appear to contradict each other. "
                    "Bob may want to review and resolve.",
                )
                found += 1

    log.info("Contradiction scan complete", found=found)
    return {"task": "contradiction", "contradictions_found": found}


# ---------------------------------------------------------------------------
# Main cycle orchestrator
# ---------------------------------------------------------------------------

async def run_cycle(pool: asyncpg.Pool) -> dict:
    """
    Run one full heartbeat cycle.
    Returns a summary dict suitable for logging and MCP response.
    """
    started_at = datetime.now(timezone.utc)
    cfg = await _read_config(pool)
    energy = await _refill_energy(pool, cfg)

    log.info("Heartbeat cycle starting",
             cycle_count=cfg.get("cycle_count", 0), energy=energy)

    tasks_run: list[dict] = []
    energy_used = 0
    notes: list[str] = []
    cycle_diary_id = None

    for task_name in TASK_ORDER:
        cost = TASK_COSTS[task_name]

        if cost > 0 and energy - energy_used < cost:
            notes.append(f"{task_name} skipped: insufficient energy "
                         f"({energy - energy_used} < {cost})")
            log.info("Task skipped: insufficient energy",
                     task=task_name, have=energy - energy_used, need=cost)
            continue

        try:
            if task_name == "maintenance":
                result = await _task_maintenance(pool)
            elif task_name == "wake_up":
                result = await _task_wake_up(pool)
                cycle_diary_id = result.get("diary_entry_id")
            elif task_name == "decay":
                result = await _task_decay(pool)
            elif task_name == "drive_monitor":
                result = await _task_drive_monitor(pool)
            elif task_name == "recollection":
                result = await _task_recollection(pool)
            elif task_name == "contradiction":
                result = await _task_contradiction_scan(pool)
            else:
                continue

            tasks_run.append(result)
            energy_used += cost

        except Exception as e:
            msg = f"{task_name} error: {e}"
            notes.append(msg)
            log.error("Task failed", task=task_name, error=str(e))

    # Persist cycle log
    completed_at = datetime.now(timezone.utc)
    diary_uuid = None
    if cycle_diary_id:
        import uuid as _uuid
        try:
            diary_uuid = _uuid.UUID(cycle_diary_id)
        except Exception:
            pass

    log_row = await pool.fetchrow(
        """INSERT INTO heartbeat_log
             (started_at, completed_at, energy_used, tasks_run, diary_entry_id, notes)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6)
           RETURNING id, cycle_number""",
        started_at, completed_at, energy_used,
        json.dumps(tasks_run),
        diary_uuid,
        "; ".join(notes) if notes else None,
    )

    # Update heartbeat_config runtime state
    new_energy = max(0, energy - energy_used)
    new_cycle_count = int(cfg.get("cycle_count", 0)) + 1
    await _set_config(pool, "energy_current", new_energy)
    await _set_config(pool, "last_run", completed_at.isoformat())
    await _set_config(pool, "cycle_count", new_cycle_count)

    duration_s = (completed_at - started_at).total_seconds()

    summary = {
        "cycle_number":   log_row["cycle_number"],
        "cycle_id":       str(log_row["id"]),
        "started_at":     started_at.isoformat(),
        "completed_at":   completed_at.isoformat(),
        "duration_s":     round(duration_s, 2),
        "energy_before":  energy,
        "energy_used":    energy_used,
        "energy_after":   new_energy,
        "tasks_run":      tasks_run,
        "notes":          notes,
    }

    log.info("Heartbeat cycle complete",
             cycle=log_row["cycle_number"],
             energy_used=energy_used,
             duration_s=round(duration_s, 2))

    return summary
