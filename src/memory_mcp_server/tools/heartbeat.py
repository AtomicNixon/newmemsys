"""MCP tools: heartbeat_status, heartbeat_configure, heartbeat_pulse, heartbeat_diagnostic."""
from __future__ import annotations

import json
from typing import Optional

import structlog

from memory_mcp_server import database as db
from memory_mcp_server import heartbeat as hb
from memory_mcp_server.tools.memory import _row_to_dict
from memory_mcp_server.tools import health as health_tools

log = structlog.get_logger(__name__)

VALID_FREQUENCIES = {"hourly", "2x_daily", "4x_daily", "daily"}

FREQUENCY_HOURS = {
    "hourly":   1,
    "2x_daily": 12,
    "4x_daily": 6,
    "daily":    24,
}


async def heartbeat_status() -> dict:
    """Return full heartbeat status: config + last 3 cycle logs."""
    pool = await db.get_pool()

    # Config
    rows = await pool.fetch("SELECT key, value FROM heartbeat_config ORDER BY key")
    config = {}
    for row in rows:
        raw = row["value"]
        try:
            config[row["key"]] = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            config[row["key"]] = raw

    # Recent cycles
    log_rows = await pool.fetch(
        """SELECT cycle_number, started_at, completed_at, energy_used,
                  tasks_run, diary_entry_id, notes
           FROM heartbeat_log
           ORDER BY started_at DESC LIMIT 3"""
    )
    recent_cycles = []
    for r in log_rows:
        d = _row_to_dict(r)
        if isinstance(d.get("tasks_run"), str):
            try:
                d["tasks_run"] = json.loads(d["tasks_run"])
            except Exception:
                pass
        recent_cycles.append(d)

    return {
        "config":        config,
        "recent_cycles": recent_cycles,
        "total_cycles":  int(config.get("cycle_count", 0)),
        "enabled":       config.get("enabled", False),
        "is_configured": config.get("is_configured", False),
    }


async def heartbeat_configure(
    enabled: Optional[bool]   = None,
    frequency: Optional[str]  = None,
    energy_budget: Optional[int] = None,
    chat_model: Optional[str] = None,
) -> dict:
    """Partial update heartbeat configuration. Only supplied fields change."""
    pool = await db.get_pool()
    changes: dict = {}
    errors: list[str] = []

    if enabled is not None:
        await pool.execute(
            "UPDATE heartbeat_config SET value = $1::jsonb WHERE key = 'enabled'",
            json.dumps(enabled),
        )
        changes["enabled"] = enabled

    if frequency is not None:
        if frequency not in VALID_FREQUENCIES:
            errors.append(f"Invalid frequency '{frequency}'. "
                          f"Valid: {sorted(VALID_FREQUENCIES)}")
        else:
            await pool.execute(
                "UPDATE heartbeat_config SET value = $1::jsonb WHERE key = 'frequency'",
                json.dumps(frequency),
            )
            changes["frequency"] = frequency

    if energy_budget is not None:
        if not (1 <= energy_budget <= 100):
            errors.append(f"energy_budget must be 1–100, got {energy_budget}")
        else:
            await pool.execute(
                "UPDATE heartbeat_config SET value = $1::jsonb WHERE key = 'energy_budget'",
                json.dumps(energy_budget),
            )
            changes["energy_budget"] = energy_budget

    if chat_model is not None:
        await pool.execute(
            "UPDATE heartbeat_config SET value = $1::jsonb WHERE key = 'chat_model'",
            json.dumps(chat_model),
        )
        changes["chat_model"] = chat_model

    result = {"changes_applied": changes}
    if errors:
        result["errors"] = errors

    # Return current state
    status = await heartbeat_status()
    result["config"] = status["config"]
    return result


async def heartbeat_pulse() -> dict:
    """Trigger one heartbeat cycle immediately, regardless of schedule."""
    pool = await db.get_pool()

    # Check is_configured
    cfg_row = await pool.fetchrow(
        "SELECT value FROM heartbeat_config WHERE key = 'is_configured'"
    )
    if cfg_row:
        val = json.loads(cfg_row["value"]) if isinstance(cfg_row["value"], str) else cfg_row["value"]
        if not val:
            return {"error": "Heartbeat not yet configured. Call heartbeat_configure first."}

    log.info("Manual heartbeat pulse triggered via MCP")
    summary = await hb.run_cycle(pool)
    return summary


async def heartbeat_diagnostic() -> dict:
    """Combined heartbeat_status + health in one call — convenient for session startup."""
    status = await heartbeat_status()
    health  = await health_tools.health()
    return {
        "heartbeat": status,
        "health":    health,
    }
