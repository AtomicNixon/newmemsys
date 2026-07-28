#!/usr/bin/env python
"""
newmemsys-doctor — Invariant checks for NewMemSys. Pass/fail, not a dashboard.

Run this after any change to the system — schema migration, code update,
config change. Verifies the system is in a healthy state and fails loud
on anything that looks wrong.

Usage:
    python newmemsys-doctor.py              # run all checks, exit 0=pass / 1=fail
    python newmemsys-doctor.py --json       # machine-readable
    python newmemsys-doctor.py --verbose    # show passing checks too

Exit codes:
    0 — all checks passed
    1 — one or more checks failed
    2 — cannot connect to database
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cli_db import connect, close, fetch, fetchrow, fetchval


def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"

GREEN  = lambda t: _c("32", t)
RED    = lambda t: _c("31", t)
YELLOW = lambda t: _c("33", t)
BOLD   = lambda t: _c("1", t)


class Check:
    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail

    def __repr__(self):
        status = GREEN("✓ PASS") if self.passed else RED("✗ FAIL")
        return f"  {status}  {self.name}{': ' + self.detail if self.detail else ''}"


async def run_doctor(verbose: bool = False) -> tuple[list[Check], int]:
    pool = await connect()
    checks: list[Check] = []
    try:
        # ── 1. Database reachable ────────────────────────────────────────────
        checks.append(Check("database reachable", True, "connected"))

        # ── 2. memories is a VIEW (computed decay refactor landed) ───────────
        rel_kind = await fetchval(
            pool,
            "SELECT relkind FROM pg_class WHERE relname = 'memories'"
        )
        # asyncpg returns bytes for char columns
        if isinstance(rel_kind, bytes):
            rel_kind = rel_kind.decode()
        if rel_kind == 'v':
            checks.append(Check("memories is a VIEW (computed decay)", True))
        elif rel_kind == 'r':
            checks.append(Check("memories is a VIEW (computed decay)", False,
                                "memories is still a table — migration 08 not applied"))
        else:
            checks.append(Check("memories is a VIEW (computed decay)", False,
                                f"unexpected relkind: {rel_kind}"))

        # ── 3. memories_base table exists ────────────────────────────────────
        base_exists = await fetchval(
            pool,
            "SELECT EXISTS(SELECT 1 FROM pg_class WHERE relname = 'memories_base')"
        )
        checks.append(Check("memories_base table exists", bool(base_exists)))

        # ── 4. importance_original column exists ─────────────────────────────
        has_orig = await fetchval(
            pool,
            """SELECT EXISTS(SELECT 1 FROM information_schema.columns
               WHERE table_name='memories_base' AND column_name='importance_original')"""
        )
        checks.append(Check("importance_original column exists", bool(has_orig)))

        # ── 5. last_recalled_at column exists ────────────────────────────────
        has_recalled = await fetchval(
            pool,
            """SELECT EXISTS(SELECT 1 FROM information_schema.columns
               WHERE table_name='memories_base' AND column_name='last_recalled_at')"""
        )
        checks.append(Check("last_recalled_at column exists", bool(has_recalled)))

        # ── 6. decay_importance() function does NOT exist (dropped) ──────────
        decay_fn_exists = await fetchval(
            pool,
            "SELECT EXISTS(SELECT 1 FROM pg_proc WHERE proname = 'decay_importance')"
        )
        checks.append(Check("decay_importance() dropped (no destructive writes)",
                            not decay_fn_exists))

        # ── 7. touch_last_recalled() function exists ─────────────────────────
        touch_fn_exists = await fetchval(
            pool,
            "SELECT EXISTS(SELECT 1 FROM pg_proc WHERE proname = 'touch_last_recalled')"
        )
        checks.append(Check("touch_last_recalled() function exists",
                            bool(touch_fn_exists)))

        # ── 8. No memories with importance_original near zero (bug-damaged) ──
        # importance_original should be 0.0 (explicitly zeroed) or >= 0.05.
        # Values in between are suspicious — likely bug damage.
        damaged_count = await fetchval(
            pool,
            """SELECT COUNT(*) FROM memories_base
               WHERE status = 'active'
                 AND importance_original > 0.0
                 AND importance_original < 0.05"""
        )
        if damaged_count == 0:
            checks.append(Check("no bug-damaged importance_original values", True))
        else:
            checks.append(Check("no bug-damaged importance_original values", False,
                                f"{damaged_count} memories have 0 < importance_original < 0.05"))

        # ── 9. INSTEAD OF triggers exist on memories view ────────────────────
        trg_count = await fetchval(
            pool,
            """SELECT COUNT(*) FROM pg_trigger
               WHERE tgrelid = 'memories'::regclass AND NOT tgisinternal"""
        )
        if trg_count >= 3:
            checks.append(Check("INSTEAD OF triggers on memories view", True,
                                f"{trg_count} triggers"))
        else:
            checks.append(Check("INSTEAD OF triggers on memories view", False,
                                f"expected >=3, found {trg_count}"))

        # ── 10. Computed importance is in valid range [0, 1] ─────────────────
        range_bad = await fetchval(
            pool,
            """SELECT COUNT(*) FROM memories
               WHERE status = 'active'
                 AND (importance < 0 OR importance > 1
                      OR importance IS NULL)"""
        )
        if range_bad == 0:
            checks.append(Check("computed importance in valid range [0,1]", True))
        else:
            checks.append(Check("computed importance in valid range [0,1]", False,
                                f"{range_bad} memories out of range"))

        # ── 11. Graph edges reference valid memories ─────────────────────────
        orphan_edges = await fetchval(
            pool,
            """SELECT COUNT(*) FROM memory_graph g
               WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = g.memory_id)
                  OR NOT EXISTS (SELECT 1 FROM memories m WHERE m.id = g.connected_memory_id)"""
        )
        if orphan_edges == 0:
            checks.append(Check("no orphan graph edges", True))
        else:
            checks.append(Check("no orphan graph edges", False,
                                f"{orphan_edges} edges reference missing memories"))

        # ── 12. Outbox not backing up (>100 pending = problem) ───────────────
        pending = await fetchval(
            pool,
            "SELECT COUNT(*) FROM outbox WHERE status = 'pending'"
        )
        if pending <= 100:
            checks.append(Check("outbox not backed up", True,
                                f"{pending} pending"))
        else:
            checks.append(Check("outbox not backed up", False,
                                f"{pending} pending items — review needed"))

        # ── 13. Heartbeat config has required keys ───────────────────────────
        required_keys = ['enabled', 'energy_budget', 'frequency', 'is_configured']
        missing_keys = []
        for key in required_keys:
            exists = await fetchval(
                pool,
                "SELECT EXISTS(SELECT 1 FROM heartbeat_config WHERE key = $1)",
                key,
            )
            if not exists:
                missing_keys.append(key)
        if not missing_keys:
            checks.append(Check("heartbeat config has required keys", True))
        else:
            checks.append(Check("heartbeat config has required keys", False,
                                f"missing: {', '.join(missing_keys)}"))

        # ── 14. Ollama reachable (embeddings depend on it) ───────────────────
        import urllib.request
        try:
            req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                json.loads(resp.read().decode("utf-8"))
            checks.append(Check("Ollama reachable", True))
        except Exception as e:
            checks.append(Check("Ollama reachable", False, str(e)[:80]))

        # ── 15. Vestige binary exists (bridge dependency) ────────────────────
        import os
        vestige_exe = r"E:\ClaudeAI\vestige.exe"
        if os.path.exists(vestige_exe):
            checks.append(Check("Vestige binary exists (bridge)", True))
        else:
            checks.append(Check("Vestige binary exists (bridge)", False,
                                f"not found at {vestige_exe}"))

    finally:
        await close(pool)

    failed = sum(1 for c in checks if not c.passed)
    return checks, failed


async def main() -> int:
    args = sys.argv[1:]
    json_mode = "--json" in args
    verbose = "--verbose" in args or "-v" in args

    try:
        checks, failed = await run_doctor(verbose=verbose)
    except SystemExit:
        raise
    except Exception as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return 2

    if json_mode:
        print(json.dumps({
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail}
                       for c in checks],
            "passed": len(checks) - failed,
            "failed": failed,
            "total": len(checks),
        }, indent=2))
    else:
        print(BOLD("\n  ╔══ NEWMEMSYS DOCTOR ══╗\n"))
        if verbose:
            for c in checks:
                print(c)
        else:
            # Only show failures
            for c in checks:
                if not c.passed:
                    print(c)
            if failed == 0:
                print(f"  {GREEN('✓ ALL CHECKS PASSED')}  ({len(checks)} checks)")
        print(f"\n  {len(checks) - failed} passed, {RED(str(failed)) if failed else GREEN('0')} failed")
        print()

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
