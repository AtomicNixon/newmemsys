#!/usr/bin/env python
"""
newmemsys-review — Retention / consolidation report for NewMemSys.

Standalone — no MCP, no Claude Code. Surfaces what NewMemSys actually tracks
(importance, half-life, decay status, valence) and flags suspicious patterns
like uniform-score clusters that look "stuck at default" from the outside.

Note: NewMemSys does NOT use FSRS — it uses importance + half_life_hours for
decay. This tool reports on that model honestly rather than fabricating an
FSRS retention number. If Bob sees "65% retention everywhere" that's coming
from Vestige, not us.

Usage:
    python newmemsys-review.py                  # full report
    python newmemsys-review.py --json           # machine-readable
    python newmemsys-review.py --decay          # only memories past half-life
    python newmemsys-review.py --consolidate    # run a consolidation pass
                                              # (decays expired memories, no deletion)
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone, timedelta
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
CYAN   = lambda t: _c("36", t)
BOLD   = lambda t: _c("1", t)


async def gather_review() -> dict:
    pool = await connect()
    try:
        # ── Importance distribution ────────────────────────────────────────
        imp_dist = await fetch(
            pool,
            """SELECT
                 CASE
                   WHEN importance < 0.1  THEN '0.0-0.1 (noise)'
                   WHEN importance < 0.3  THEN '0.1-0.3 (low)'
                   WHEN importance < 0.5  THEN '0.3-0.5 (mid)'
                   WHEN importance < 0.7  THEN '0.5-0.7 (high)'
                   WHEN importance < 0.9  THEN '0.7-0.9 (load-bearing)'
                   ELSE                        '0.9-1.0 (critical)'
                 END AS bucket,
                 COUNT(*) AS n,
                 ROUND(AVG(importance)::numeric, 4) AS avg_imp
               FROM memories WHERE status='active'
               GROUP BY bucket ORDER BY bucket"""
        )

        # ── Half-life buckets ──────────────────────────────────────────────
        hl_dist = await fetch(
            pool,
            """SELECT
                 CASE
                   WHEN half_life_hours < 168   THEN '< 1 week'
                   WHEN half_life_hours < 720   THEN '1 week – 1 month'
                   WHEN half_life_hours < 2160  THEN '1-3 months'
                   WHEN half_life_hours < 8760  THEN '3-12 months'
                   ELSE                              '> 1 year'
                 END AS bucket,
                 COUNT(*) AS n
               FROM memories WHERE status='active'
               GROUP BY bucket ORDER BY MIN(half_life_hours)"""
        )

        # ── Decay status: memories past their half-life ────────────────────
        # A memory is "due for review" if it's older than its half_life_hours
        # and hasn't been touched (updated_at) in that long.
        decay_rows = await fetch(
            pool,
            """SELECT id, content, type, importance, emotional_valence,
                      half_life_hours, created_at, updated_at,
                      EXTRACT(EPOCH FROM (NOW() - updated_at))/3600 AS hours_since_touch
               FROM memories
               WHERE status='active'
                 AND EXTRACT(EPOCH FROM (NOW() - updated_at))/3600 > half_life_hours
               ORDER BY hours_since_touch DESC
               LIMIT 50"""
        )

        # ── Uniform-score suspicion ────────────────────────────────────────
        # Flag importance values that appear suspiciously often (possible
        # "stuck at default" — e.g. 1006 memories all at 0.5).
        uniform_rows = await fetch(
            pool,
            """SELECT ROUND(importance::numeric, 2) AS imp_val,
                      COUNT(*) AS n
               FROM memories WHERE status='active'
               GROUP BY ROUND(importance::numeric, 2)
               HAVING COUNT(*) > 5
               ORDER BY n DESC LIMIT 10"""
        )
        total_active = await fetchval(
            pool,
            "SELECT COUNT(*) FROM memories WHERE status='active'"
        )
        # Flag any value that accounts for >30% of all memories
        suspicious_uniform = [
            {"imp_val": float(r["imp_val"]), "n": r["n"],
             "pct": round(r["n"] / total_active * 100, 1)}
            for r in uniform_rows if r["n"] / total_active > 0.30
        ]

        # ── Valence distribution ───────────────────────────────────────────
        val_dist = await fetch(
            pool,
            """SELECT
                 CASE
                   WHEN emotional_valence < -0.3 THEN 'negative'
                   WHEN emotional_valence < 0.3  THEN 'neutral'
                   ELSE                               'positive'
                 END AS bucket,
                 COUNT(*) AS n,
                 ROUND(AVG(emotional_valence)::numeric, 4) AS avg_val
               FROM memories WHERE status='active'
               GROUP BY bucket ORDER BY bucket"""
        )

        # ── Type × importance cross-tab ────────────────────────────────────
        type_imp = await fetch(
            pool,
            """SELECT type, COUNT(*) AS n,
                      ROUND(AVG(importance)::numeric, 4) AS avg_imp,
                      ROUND(MIN(importance)::numeric, 4) AS min_imp,
                      ROUND(MAX(importance)::numeric, 4) AS max_imp,
                      ROUND(STDDEV(importance)::numeric, 4) AS stddev_imp
               FROM memories WHERE status='active'
               GROUP BY type ORDER BY n DESC"""
        )

        # ── Zero-importance memories (noise candidates) ────────────────────
        zero_imp = await fetchval(
            pool,
            """SELECT COUNT(*) FROM memories
               WHERE status='active' AND importance < 0.05"""
        )

        # ── Bridge coverage by importance ──────────────────────────────────
        bridge_by_imp = await fetch(
            pool,
            """SELECT
                 CASE WHEN vestige_node_id IS NOT NULL THEN 'bridged' ELSE 'unbridged' END AS state,
                 COUNT(*) AS n,
                 ROUND(AVG(importance)::numeric, 4) AS avg_imp
               FROM memories WHERE status='active'
               GROUP BY state ORDER BY state"""
        )

        return {
            "total_active":         total_active,
            "importance_dist":      [{"bucket": r["bucket"], "n": r["n"],
                                      "avg_imp": float(r["avg_imp"]) if r["avg_imp"] else None}
                                     for r in imp_dist],
            "halflife_dist":        [{"bucket": r["bucket"], "n": r["n"]} for r in hl_dist],
            "decay_due":            [{"id": str(r["id"]),
                                      "content": r["content"][:120],
                                      "type": r["type"],
                                      "importance": float(r["importance"]),
                                      "half_life_hours": r["half_life_hours"],
                                      "hours_since_touch": float(r["hours_since_touch"])}
                                     for r in decay_rows],
            "uniform_suspicion":    suspicious_uniform,
            "valence_dist":         [{"bucket": r["bucket"], "n": r["n"],
                                      "avg_val": float(r["avg_val"]) if r["avg_val"] else None}
                                     for r in val_dist],
            "type_importance":      [{"type": r["type"], "n": r["n"],
                                      "avg_imp": float(r["avg_imp"]) if r["avg_imp"] else None,
                                      "min_imp": float(r["min_imp"]) if r["min_imp"] else None,
                                      "max_imp": float(r["max_imp"]) if r["max_imp"] else None,
                                      "stddev_imp": float(r["stddev_imp"]) if r["stddev_imp"] else None}
                                     for r in type_imp],
            "zero_importance_count": zero_imp,
            "bridge_by_importance": [{"state": r["state"], "n": r["n"],
                                      "avg_imp": float(r["avg_imp"]) if r["avg_imp"] else None}
                                     for r in bridge_by_imp],
        }
    finally:
        await close(pool)


async def consolidate() -> dict:
    """Run a consolidation pass: decay expired memories.

    Marks memories as 'expired' if they are:
    - importance < 0.1 (noise)
    - older than 2x their half_life_hours
    - not touched in that long

    Does NOT delete anything — expired is a soft state, reversible.
    """
    pool = await connect()
    try:
        result = await pool.execute(
            """UPDATE memories
               SET status = 'expired'
               WHERE status = 'active'
                 AND importance < 0.1
                 AND EXTRACT(EPOCH FROM (NOW() - updated_at))/3600 > (half_life_hours * 2)
               RETURNING id"""
        )
        # asyncpg returns "UPDATE N" — extract count
        n = int(result.split()[-1]) if result else 0
        return {"consolidated": n, "action": "expired (soft, reversible)"}
    finally:
        await close(pool)


def _print_review(r: dict) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(BOLD(f"\n  ╔══ NEWMEMSYS REVIEW ══ {now} ══╗\n"))

    print(BOLD("  OVERVIEW"))
    print(f"    active memories: {r['total_active']}")
    print(f"    zero-importance (<0.05): {r['zero_importance_count']}")
    print()

    # ── Importance distribution ───────────────────────────────────────────
    print(BOLD("  IMPORTANCE DISTRIBUTION"))
    for row in r["importance_dist"]:
        pct = row["n"] / r["total_active"] * 100 if r["total_active"] else 0
        bar = "█" * int(pct / 2)
        print(f"    {row['bucket']:<24} {row['n']:>5}  ({pct:5.1f}%)  {bar}")
    print()

    # ── Uniform-score suspicion ───────────────────────────────────────────
    print(BOLD("  UNIFORM-SCORE SUSPICION"))
    if r["uniform_suspicion"]:
        for s in r["uniform_suspicion"]:
            print(f"    {YELLOW('⚠')}  importance={s['imp_val']:.2f} appears {s['n']} times "
                  f"({s['pct']}% of all memories) — possible 'stuck at default'")
    else:
        print(f"    {GREEN('✓')}  no single importance value dominates (>30%)")
    print()

    # ── Half-life distribution ────────────────────────────────────────────
    print(BOLD("  HALF-LIFE DISTRIBUTION"))
    for row in r["halflife_dist"]:
        print(f"    {row['bucket']:<24} {row['n']:>5}")
    print()

    # ── Decay: memories due for review ────────────────────────────────────
    print(BOLD(f"  DECAY — DUE FOR REVIEW ({len(r['decay_due'])} shown, top 50)"))
    if r["decay_due"]:
        for d in r["decay_due"][:20]:
            age_days = (d["hours_since_touch"] or 0) / 24
            hl_days  = (d["half_life_hours"] or 0) / 24
            imp      = d["importance"] or 0
            print(f"    {d['type']:<11} imp={imp:.2f}  "
                  f"age={age_days:.0f}d  hl={hl_days:.0f}d  "
                  f"{d['content'][:70]}")
        if len(r["decay_due"]) > 20:
            print(f"    ... and {len(r['decay_due']) - 20} more")
    else:
        print(f"    {GREEN('✓')}  no memories past their half-life")
    print()

    # ── Valence ───────────────────────────────────────────────────────────
    print(BOLD("  VALENCE DISTRIBUTION"))
    for row in r["valence_dist"]:
        v = f"{row['avg_val']:.4f}" if row["avg_val"] is not None else "—"
        print(f"    {row['bucket']:<10} {row['n']:>5}  avg_val={v}")
    print()

    # ── Type × importance ─────────────────────────────────────────────────
    print(BOLD("  TYPE × IMPORTANCE"))
    print(f"    {'type':<12} {'n':>5}  {'avg':>6}  {'min':>6}  {'max':>6}  {'stddev':>6}")
    for row in r["type_importance"]:
        def _f(v):
            return f"{v:.3f}" if v is not None else "—"
        print(f"    {row['type']:<12} {row['n']:>5}  {_f(row['avg_imp'])}  "
              f"{_f(row['min_imp'])}  {_f(row['max_imp'])}  {_f(row['stddev_imp'])}")
    print()

    # ── Bridge coverage ───────────────────────────────────────────────────
    print(BOLD("  BRIDGE COVERAGE (by importance)"))
    for row in r["bridge_by_importance"]:
        v = f"{row['avg_imp']:.4f}" if row["avg_imp"] is not None else "—"
        print(f"    {row['state']:<10} {row['n']:>5}  avg_imp={v}")
    print()

    # ── Note on FSRS ──────────────────────────────────────────────────────
    print(_c("2",
        "  NOTE: NewMemSys uses importance + half_life_hours for decay, not FSRS.\n"
        "  If you see a uniform '65% retention' metric, that's coming from Vestige,\n"
        "  not from this database. This report shows what NewMemSys actually tracks.\n"))


async def main() -> int:
    args = sys.argv[1:]

    if "--consolidate" in args:
        result = await consolidate()
        print(f"Consolidation pass complete: {result['consolidated']} memories {result['action']}")
        return 0

    json_mode = "--json" in args
    decay_only = "--decay" in args

    try:
        review = await gather_review()
    except SystemExit:
        raise
    except Exception as e:
        sys.stderr.write(f"ERROR gathering review: {e}\n")
        return 1

    if json_mode:
        print(json.dumps(review, indent=2, default=str))
    elif decay_only:
        for d in review["decay_due"]:
            print(f"{d['id']}  imp={d['importance']:.2f}  {d['content']}")
    else:
        _print_review(review)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
