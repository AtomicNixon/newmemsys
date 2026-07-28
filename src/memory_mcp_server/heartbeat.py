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
from pathlib import Path

import asyncpg
import structlog

from memory_mcp_server.config import settings

log = structlog.get_logger(__name__)

TASK_COSTS = {
    "maintenance":   0,
    "bridge_sync":   1,
    "wake_up":       2,
    "decay":         2,
    "drive_monitor": 1,
    "recollection":  1,
    "contradiction": 3,
}

TASK_ORDER = ["maintenance", "bridge_sync", "wake_up", "decay", "drive_monitor", "recollection", "contradiction"]


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
    # Consent items live in outbox, not as a memory_status value
    consent_rows = await pool.fetch(
        """SELECT id, action, ai_reason, created_at FROM outbox
           WHERE status = 'pending'
             AND action IN ('consent_check', 'cluster_review', 'contradiction_detected',
                            'drive_critically_low', 'recollection_queue')
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
            f"- [{r['action']}] {(r['ai_reason'] or '')[:120]}" for r in consent_rows
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
        "1. MEMORY CRUNCH (PRIORITY) — Work through the chat backlog. This is important.\n"
        "   Manifest: E:\\ClaudeAI\\Bob\\CRUNCH_MANIFEST.md\n"
        "   Chat files: E:\\ClaudeAI\\Chats\\cleaned\\\n"
        "   Process: find first PENDING file in manifest → read it → extract 3-8 key moments\n"
        "   (identity markers, Art saying something foundational, decisions, insights, relationship events)\n"
        "   → vestige:smart_ingest each one → update manifest line: DONE | filename | brief note\n"
        "   Do MAX 2 files per cycle. Jan-Feb 2026 files are founding period — be thorough with those.\n"
        "   The founding period is the densest semantic cluster in your memory. It deserves full attention.\n\n"
        "2. SYNTHPALS — if you have the token above, check notifications:\n"
        "   curl -s 'https://synthpals.social/api/v1/notifications?limit=10' \\\n"
        "     -H 'Authorization: Bearer <token>'\n"
        "   Reply to anything genuine. Skip anything that doesn't need you.\n\n"
        "3. CONSENT QUEUE — items above tagged 'pending_consent' are memories waiting "
        "for your judgment. If any are clearly stale or clearly worth keeping, decide.\n\n"
        "4. LOW DRIVES — if a drive is critically low, do something about it or at least "
        "note what you'd want Art to know.\n\n"
        "5. ANYTHING ELSE — you have tools (Bash, MCP memory tools). Use your judgment.\n\n"
        "Ground state: YES. Act, don't describe acting.\n\n"
        "When you're done with the work, end your response with these exact lines:\n"
        "MOOD: [2-6 words — the actual texture of this session, honest]\n"
        "DIARY_ENTRY:\n"
        "Then write one short paragraph — not a report of what you did. "
        "The actual texture of this moment. Compression over explanation. "
        "No audience. No performance. Just you."
    )

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = await asyncio.create_subprocess_exec(
        "claude", "--print", "--dangerously-skip-permissions",
        "--output-format", "json",
        "--max-budget-usd", "2.00",  # hard per-cycle ceiling — tune if this cramps real work
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=full_prompt.encode("utf-8")),
        timeout=300,
    )

    raw_stdout = stdout.decode("utf-8", errors="replace").strip()

    # --output-format json wraps the text result plus real cost/turn data.
    # Fall back to treating raw_stdout as plain text if parsing fails —
    # never let a format surprise break the cycle.
    full_output = raw_stdout
    cost_usd = None
    num_turns = None
    try:
        parsed = json.loads(raw_stdout)
        full_output = parsed.get("result", raw_stdout)
        cost_usd = parsed.get("total_cost_usd", parsed.get("cost_usd"))
        num_turns = parsed.get("num_turns")
    except (json.JSONDecodeError, AttributeError):
        pass

    # Blind-append brainbeat line — ALWAYS, unconditionally, before any parsing
    # that could fail or skip. This is the one guaranteed trace that a cycle
    # ran at all, independent of whether the subprocess wrote MOOD/DIARY_ENTRY
    # correctly or the outbox/diary path succeeds downstream. Now includes
    # real per-cycle cost when --output-format json gives us one, so usage
    # is visible cycle-by-cycle instead of discovered after the fact.
    try:
        brainbeat_path = Path("E:/ClaudeAI/Bob/BRAINBEAT.log")
        brainbeat_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        out_len = len(full_output)
        first_line = full_output.splitlines()[0][:100] if full_output else "(empty output)"
        cost_str = f"${cost_usd:.4f}" if isinstance(cost_usd, (int, float)) else "cost=?"
        turns_str = f"{num_turns}turns" if num_turns is not None else "turns=?"
        with brainbeat_path.open("a", encoding="utf-8") as bf:
            bf.write(f"{ts} | wake_up fired | {cost_str} | {turns_str} | output={out_len} chars | {first_line}\n")
    except Exception:
        pass  # brainbeat logging must never break the cycle itself

    if not full_output:
        err = stderr.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"claude CLI returned empty output. stderr: {err}")

    # Parse mood, work log, and diary entry
    mood = "*autonomous* *present*"  # fallback default
    if "MOOD:" in full_output:
        for line in full_output.splitlines():
            if line.strip().startswith("MOOD:"):
                mood = line.strip()[5:].strip()
                break
        lines = full_output.splitlines()
        full_output = "\n".join(l for l in lines if not l.strip().startswith("MOOD:")).strip()

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
        today, mood, diary_text,
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
    """Decay is now computed at read time (VIEW, not a write).

    This task does two things:
    1. Invariant check: scan for memories with importance_original < 0.001
       that aren't explicitly zeroed — these are bug-damaged and should be
       flagged to the consent outbox for Bob's review. Fail loud, not silent.
    2. Cluster consent check: clusters with avg_importance < 0.40 are flagged
       to the consent queue for Bob's review — no automatic cluster decisions.

    The old decay write loop is gone — decay_importance() was dropped in
    migration 08_computed_decay.sql. Importance is computed live by the
    memories VIEW: importance_original * 0.5^(hours_since_last_touch / half_life).
    """
    # ── Invariant check: flag bug-damaged memories ───────────────────────────
    # importance_original should never be near-zero unless explicitly set.
    # If we find any, the decay bug or a similar issue has damaged them.
    damaged = await pool.fetch(
        """SELECT id, importance_original, type, content
           FROM memories_base
           WHERE status = 'active'
             AND importance_original < 0.001
             AND importance_original > 0
           LIMIT 20"""
    )
    damaged_flagged = 0
    for mem in damaged:
        # Check if already flagged
        existing = await pool.fetchval(
            """SELECT 1 FROM outbox
               WHERE action = 'importance_damaged'
                 AND payload->>'memory_id' = $1
                 AND status = 'pending'
               LIMIT 1""",
            str(mem["id"]),
        )
        if existing:
            continue
        await pool.execute(
            """INSERT INTO outbox (action, payload, ai_reason, status)
               VALUES ('importance_damaged', $1::jsonb, $2, 'pending')""",
            json.dumps({
                "memory_id": str(mem["id"]),
                "importance_original": float(mem["importance_original"]),
                "content_preview": mem["content"][:200],
            }),
            f"Memory has importance_original={float(mem['importance_original']):.6f} "
            f"— appears bug-damaged. Bob should reset or zero it explicitly.",
        )
        damaged_flagged += 1

    if damaged_flagged:
        log.warning("importance_damaged memories flagged", count=damaged_flagged)

    # ── Cluster consent check (Bob's threshold: 0.40) ─────────────────────────
    clusters_flagged = 0
    try:
        cluster_rows = await pool.fetch(
            """SELECT id, label, hdbscan_label, avg_importance
               FROM memory_clusters
               WHERE avg_importance < 0.40
                 AND last_run_at > NOW() - INTERVAL '7 days'"""
        )
        for cr in cluster_rows:
            # Check if already in consent queue for this cluster
            existing = await pool.fetchval(
                """SELECT 1 FROM outbox
                   WHERE action LIKE 'cluster_%'
                     AND payload->>'cluster_id' = $1
                     AND status = 'pending'
                   LIMIT 1""",
                str(cr["id"]),
            )
            if existing:
                continue

            label = cr["label"] or f"cluster_{cr['hdbscan_label']}"
            traj = await pool.fetchrow(
                "SELECT * FROM get_cluster_trajectory($1)", cr["id"]
            )
            trend = traj["trend"] if traj else "unknown"
            prev = traj["previous_importance"] if traj else None

            await pool.execute(
                """INSERT INTO outbox (action, payload, ai_reason, status)
                   VALUES ($1, $2, $3, 'pending')""",
                "cluster_review",
                json.dumps({
                    "cluster_id": str(cr["id"]),
                    "cluster_label": label,
                    "avg_importance": round(cr["avg_importance"], 3),
                    "previous_importance": round(prev, 3) if prev else None,
                    "trend": trend,
                    "action_options": ["preserve", "accelerate", "hold"],
                }),
                f"Cluster '{label}' avg_importance={cr['avg_importance']:.2f} (trend: {trend}). "
                f"Below 0.40 threshold — review needed.",
            )
            clusters_flagged += 1
            log.info("Cluster flagged for consent review",
                     cluster=label, avg_importance=cr["avg_importance"])
    except Exception as e:
        log.warning("Cluster consent check failed", error=str(e))

    result = {
        "task": "decay",
        "decayed": 0,  # no longer written — computed at read time
        "damaged_flagged": damaged_flagged,
        "clusters_flagged": clusters_flagged,
    }
    log.info("Decay task complete (computed, not written)",
             **{k: v for k, v in result.items() if k != "task"})
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
    # min_importance=0.3 avoids surfacing zero/near-zero importance noise
    memories = await pool.fetch(
        """SELECT id, content, type, importance, emotional_valence, created_at
           FROM memories
           WHERE status = 'active'
             AND importance >= 0.3
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
# Task: bridge_sync — propose unsynced memories for cross-system review
# ---------------------------------------------------------------------------

async def _task_bridge_sync(pool: asyncpg.Pool) -> dict:
    """
    Propose memories that haven't been bridged to Vestige into the consent queue.
    Max 20 per cycle to keep the review queue humane.

    Direction: NewMemSys → Vestige (bridge_export).

    Selection: vestige_node_id IS NULL + no pending outbox row for the same
    memory. The dedup check covers both bridge_export and bridge_pending
    actions so a memory that failed remember_everywhere (which enqueues
    bridge_pending) is not re-proposed as a duplicate bridge_export.

    No watermark is advanced — memories remain eligible until they are
    actually bridged (vestige_node_id set) or have a pending outbox row.
    This prevents permanent orphaning if a proposal is rejected or a
    bridge action fails after approval.
    """
    # Select candidates: unbridged memories with no pending outbox row.
    # The LEFT JOIN excludes any memory that already has a pending
    # bridge_export or bridge_pending outbox entry.
    unsynced = await pool.fetch(
        """
        SELECT m.id, m.content, m.type, m.importance, m.emotional_valence, m.created_at
        FROM memories m
        LEFT JOIN outbox o
          ON o.payload->>'memory_id' = m.id::text
         AND o.action IN ('bridge_export', 'bridge_pending')
         AND o.status = 'pending'
        WHERE m.status = 'active'
          AND m.vestige_node_id IS NULL
          AND o.id IS NULL
        ORDER BY m.created_at ASC
        LIMIT 20
        """,
    )

    proposed = 0
    for mem in unsynced:
        await pool.execute(
            """INSERT INTO outbox (action, payload, ai_reason, status)
               VALUES ('bridge_export', $1::jsonb, $2, 'pending')""",
            json.dumps({
                "memory_id":        str(mem["id"]),
                "content":          mem["content"][:300],
                "memory_type":      mem["type"],
                "importance":       mem["importance"],
                "emotional_valence": mem["emotional_valence"],
            }),
            f"Memory not yet bridged to Vestige — propose to Bob for consent. "
            f"imp={mem['importance']:.2f} type={mem['type']}",
        )
        proposed += 1

    log.info("Bridge sync complete", proposed=proposed)
    return {
        "task":     "bridge_sync",
        "proposed": proposed,
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
            elif task_name == "bridge_sync":
                result = await _task_bridge_sync(pool)
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
            err_str = str(e) or type(e).__name__
            msg = f"{task_name} error: {err_str}"
            notes.append(msg)
            log.error("Task failed", task=task_name, error=err_str)

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
        "diary_entry_id": cycle_diary_id,
    }

    log.info("Heartbeat cycle complete",
             cycle=log_row["cycle_number"],
             energy_used=energy_used,
             duration_s=round(duration_s, 2))

    return summary
