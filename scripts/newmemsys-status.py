#!/usr/bin/env python
"""
newmemsys-status — Standalone dashboard for NewMemSys.

Prints a human-readable summary of the system state directly from Postgres.
No MCP session, no Claude Code, no daemon dependency. Works any time the
Docker container `newmemsys_brain` is running.

Usage:
    python newmemsys-status.py
    python newmemsys-status.py --json     # machine-readable output

Relies on cli_db.py for connection params (env vars or Docker defaults).
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from anywhere — add scripts dir to path
sys.path.insert(0, str(Path(__file__).parent))

import asyncpg
from cli_db import connect, close, fetch, fetchrow, fetchval


# ── ANSI colors (Windows 10+ supports them; degrade gracefully) ─────────────
def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"

GREEN  = lambda t: _c("32", t)
RED    = lambda t: _c("31", t)
YELLOW = lambda t: _c("33", t)
CYAN   = lambda t: _c("36", t)
BOLD   = lambda t: _c("1", t)
DIM    = lambda t: _c("2", t)


async def _check_ollama() -> dict:
    """Best-effort Ollama reachability check."""
    import urllib.request
    host = "http://localhost:11434"
    try:
        req = urllib.request.Request(f"{host}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "?") for m in data.get("models", [])]
            return {"reachable": True, "models": models}
    except Exception as e:
        return {"reachable": False, "error": str(e)[:120]}


async def _check_daemon(pool) -> dict:
    """Infer daemon status from heartbeat_config + heartbeat_log.

    We can't see the process directly, but we can tell if it's been cycling.
    """
    rows = await fetch(pool, "SELECT key, value FROM heartbeat_config")
    cfg = {r["key"]: r["value"] for r in rows}

    enabled = cfg.get("enabled") in (True, "true", "True")
    is_configured = cfg.get("is_configured") in (True, "true", "True")
    last_run_raw = cfg.get("last_run")
    next_run_raw = cfg.get("next_run")
    cycle_count = cfg.get("cycle_count", 0)
    energy_current = cfg.get("energy_current", 0)
    energy_budget = cfg.get("energy_budget", 0)

    # Parse last_run
    last_run = None
    if last_run_raw and last_run_raw not in ("null", "None", None):
        try:
            raw = json.loads(last_run_raw) if isinstance(last_run_raw, str) else last_run_raw
            last_run = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            pass

    # Parse next_run
    next_run = None
    if next_run_raw and next_run_raw not in ("null", "None", None):
        try:
            raw = json.loads(next_run_raw) if isinstance(next_run_raw, str) else next_run_raw
            next_run = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            pass

    # Heuristic: if last_run is older than 2x the expected interval, daemon
    # is probably not running. 4x/daily = 6h apart; stale = >12h.
    now = datetime.now(timezone.utc)
    stale = False
    if last_run:
        age_hours = (now - last_run).total_seconds() / 3600
        stale = age_hours > 12

    return {
        "enabled":          enabled,
        "is_configured":    is_configured,
        "cycle_count":      cycle_count,
        "energy_current":   energy_current,
        "energy_budget":    energy_budget,
        "last_run":         last_run.isoformat() if last_run else None,
        "next_run":         next_run.isoformat() if next_run else None,
        "stale":            stale,
    }


async def gather_status() -> dict:
    pool = await connect()
    try:
        # Memory counts
        mem_counts = await fetch(
            pool,
            """SELECT status, COUNT(*) AS n,
                      ROUND(AVG(importance)::numeric, 4) AS avg_imp,
                      ROUND(AVG(emotional_valence)::numeric, 4) AS avg_val
               FROM memories GROUP BY status ORDER BY status"""
        )
        total = sum(r["n"] for r in mem_counts)
        active_row = next((r for r in mem_counts if r["status"] == "active"), None)
        active_n = active_row["n"] if active_row else 0

        # Type breakdown (active only)
        type_rows = await fetch(
            pool,
            """SELECT type, COUNT(*) AS n, ROUND(AVG(importance)::numeric, 4) AS avg_imp
               FROM memories WHERE status='active' GROUP BY type ORDER BY n DESC"""
        )

        # Bridge status
        bridge_row = await fetchrow(
            pool,
            """SELECT
                 COUNT(*) FILTER (WHERE vestige_node_id IS NOT NULL) AS bridged,
                 COUNT(*) FILTER (WHERE vestige_node_id IS NULL AND status='active') AS unbridged
               FROM memories"""
        )

        # Graph
        graph_row = await fetchrow(
            pool,
            "SELECT COUNT(*) AS edges FROM memory_graph"
        )

        # Outbox pending
        outbox_rows = await fetch(
            pool,
            """SELECT action, COUNT(*) AS n
               FROM outbox WHERE status='pending'
               GROUP BY action ORDER BY n DESC"""
        )
        pending_total = sum(r["n"] for r in outbox_rows)

        # Diary
        diary_row = await fetchrow(
            pool,
            "SELECT COUNT(*) AS n, MAX(date) AS latest FROM diary"
        )

        # Worldview
        wv_row = await fetchrow(pool, "SELECT COUNT(*) AS n FROM worldview")

        # Clusters
        cluster_row = await fetchrow(pool, "SELECT COUNT(*) AS n FROM memory_clusters")

        # Daemon + Ollama (in parallel)
        daemon_task = asyncio.create_task(_check_daemon(pool))
        ollama_task = asyncio.create_task(_check_ollama())
        daemon = await daemon_task
        ollama = await ollama_task

        return {
            "memories": {
                "total":   total,
                "active":  active_n,
                "by_status":  [{"status": r["status"], "n": r["n"],
                                "avg_imp": float(r["avg_imp"]) if r["avg_imp"] else None,
                                "avg_val": float(r["avg_val"]) if r["avg_val"] else None}
                               for r in mem_counts],
                "by_type": [{"type": r["type"], "n": r["n"],
                             "avg_imp": float(r["avg_imp"]) if r["avg_imp"] else None}
                            for r in type_rows],
            },
            "bridge": {
                "bridged":   bridge_row["bridged"],
                "unbridged": bridge_row["unbridged"],
            },
            "graph": {
                "edges": graph_row["edges"],
            },
            "outbox": {
                "pending_total": pending_total,
                "by_action": [{"action": r["action"], "n": r["n"]} for r in outbox_rows],
            },
            "diary": {
                "count": diary_row["n"],
                "latest": str(diary_row["latest"]) if diary_row["latest"] else None,
            },
            "worldview": {"count": wv_row["n"]},
            "clusters":  {"count": cluster_row["n"]},
            "daemon":    daemon,
            "ollama":    ollama,
        }
    finally:
        await close(pool)


def _print_dashboard(s: dict) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(BOLD(f"\n  ╔══ NEWMEMSYS STATUS ══ {now} ══╗\n"))

    # ── Memories ───────────────────────────────────────────────────────────
    m = s["memories"]
    print(BOLD("  MEMORIES"))
    print(f"    total: {m['total']}   active: {GREEN(str(m['active']))}   "
          f"by status:")
    for r in m["by_status"]:
        imp = f"avg_imp={r['avg_imp']:.3f}" if r["avg_imp"] is not None else ""
        print(f"      {r['status']:<10} {r['n']:>5}  {imp}")
    print(f"    by type (active):")
    for r in m["by_type"]:
        imp = f"avg_imp={r['avg_imp']:.3f}" if r["avg_imp"] is not None else ""
        print(f"      {r['type']:<12} {r['n']:>5}  {imp}")
    print()

    # ── Bridge ─────────────────────────────────────────────────────────────
    b = s["bridge"]
    pct = (b["bridged"] / (b["bridged"] + b["unbridged"]) * 100) if (b["bridged"] + b["unbridged"]) else 0
    print(BOLD("  BRIDGE (Vestige)"))
    print(f"    bridged:   {b['bridged']}  ({pct:.1f}%)")
    print(f"    unbridged: {b['unbridged']}")
    print()

    # ── Graph ──────────────────────────────────────────────────────────────
    print(BOLD("  GRAPH"))
    print(f"    edges: {s['graph']['edges']}")
    print()

    # ── Outbox ─────────────────────────────────────────────────────────────
    o = s["outbox"]
    print(BOLD("  OUTBOX (pending)"))
    print(f"    total pending: {YELLOW(str(o['pending_total'])) if o['pending_total'] else GREEN('0')}")
    for r in o["by_action"]:
        print(f"      {r['action']:<28} {r['n']:>4}")
    print()

    # ── Diary / Worldview / Clusters ───────────────────────────────────────
    print(BOLD("  OTHER"))
    print(f"    diary entries: {s['diary']['count']}  (latest: {s['diary']['latest']})")
    print(f"    worldview:     {s['worldview']['count']}")
    print(f"    clusters:      {s['clusters']['count']}")
    print()

    # ── Daemon ─────────────────────────────────────────────────────────────
    d = s["daemon"]
    print(BOLD("  HEARTBEAT DAEMON"))
    if not d["enabled"]:
        print(f"    {RED('DISABLED')}  (enabled=false in heartbeat_config)")
    elif d["stale"]:
        print(f"    {YELLOW('STALE')}  — last_run is >12h old, daemon probably not running")
    else:
        print(f"    {GREEN('RUNNING')}  (inferred from recent last_run)")
    print(f"    cycle_count:    {d['cycle_count']}")
    print(f"    energy:         {d['energy_current']}/{d['energy_budget']}")
    print(f"    last_run:       {d['last_run'] or 'never'}")
    print(f"    next_run:       {d['next_run'] or 'unknown'}")
    print()

    # ── Ollama ─────────────────────────────────────────────────────────────
    ol = s["ollama"]
    print(BOLD("  OLLAMA"))
    if ol["reachable"]:
        models = ", ".join(ol["models"][:5])
        more = f" (+{len(ol['models'])-5} more)" if len(ol["models"]) > 5 else ""
        print(f"    {GREEN('REACHABLE')}  models: {models}{more}")
    else:
        print(f"    {RED('UNREACHABLE')}  {ol.get('error','')}")
    print()


async def main(json_output: bool = False) -> int:
    try:
        status = await gather_status()
    except SystemExit:
        raise
    except Exception as e:
        sys.stderr.write(f"ERROR gathering status: {e}\n")
        return 1

    if json_output:
        print(json.dumps(status, indent=2, default=str))
    else:
        _print_dashboard(status)
    return 0


if __name__ == "__main__":
    json_mode = "--json" in sys.argv
    sys.exit(asyncio.run(main(json_output=json_mode)))
