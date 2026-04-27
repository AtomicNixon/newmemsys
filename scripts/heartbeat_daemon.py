"""
heartbeat_daemon.py — Persistent scheduler process.

Fixes applied (v1.2):
  - next_run written BEFORE the cycle starts, not after
  - Minimum interval guard (75% of frequency) prevents burst-firing
  - PostgreSQL advisory lock prevents multiple simultaneous instances
  - Energy refills to full budget at cycle start (not hourly trickle)

Usage:
    python scripts/heartbeat_daemon.py
    -- or double-click --
    scripts/start_heartbeat.bat
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("POSTGRES_HOST",     "localhost")
os.environ.setdefault("POSTGRES_PORT",     "5433")
os.environ.setdefault("POSTGRES_DB",       "memory_system")
os.environ.setdefault("POSTGRES_USER",     "memory_user")
os.environ.setdefault("POSTGRES_PASSWORD", "memsys_secure_2026")
os.environ.setdefault("OLLAMA_BASE_URL",   "http://localhost:11434")
os.environ.setdefault("OLLAMA_EMBED_MODEL","nomic-embed-text")
os.environ.setdefault("ANTHROPIC_MODEL",   "claude-sonnet-4-6")

import structlog
import asyncpg

from memory_mcp_server.config import settings
from memory_mcp_server import heartbeat as hb

log = structlog.get_logger("heartbeat_daemon")

FREQUENCY_HOURS = {
    "30min":    0.5,
    "hourly":   1,
    "2x_daily": 12,
    "4x_daily": 6,
    "daily":    24,
}

# Minimum fraction of the scheduled interval that must elapse before
# another cycle is allowed. Prevents burst-firing on restart.
MIN_INTERVAL_FRACTION = 0.75

# PostgreSQL advisory lock key — prevents multiple daemon instances.
# Arbitrary but stable integer, unique to this daemon.
ADVISORY_LOCK_KEY = 7734920001

_shutdown = asyncio.Event()
_in_cycle = False


def _handle_signal(sig, frame):
    log.info("Shutdown signal received", signal=sig)
    _shutdown.set()


signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


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


def _next_run_from_now(frequency: str) -> datetime:
    hours = FREQUENCY_HOURS.get(frequency, 6)
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _min_interval_seconds(frequency: str) -> float:
    hours = FREQUENCY_HOURS.get(frequency, 6)
    return hours * 3600 * MIN_INTERVAL_FRACTION


async def _sleep_until(target: datetime) -> bool:
    """Sleep until target. Returns False if shutdown triggered."""
    now = datetime.now(timezone.utc)
    remaining = (target - now).total_seconds()
    if remaining <= 0:
        return not _shutdown.is_set()

    log.info("Sleeping until next cycle",
             next_run=target.strftime("%Y-%m-%d %H:%M UTC"),
             hours=round(remaining / 3600, 2))

    while remaining > 0 and not _shutdown.is_set():
        await asyncio.sleep(min(10, remaining))
        remaining = (target - datetime.now(timezone.utc)).total_seconds()

    return not _shutdown.is_set()


async def main() -> None:
    global _in_cycle

    print()
    print("=" * 60)
    print("  NewMemSys Heartbeat Daemon  v1.2")
    print(f"  DB  : {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}")
    print(f"  AI  : Anthropic {settings.anthropic_model}")
    print("=" * 60)
    print()

    # Connect
    try:
        pool = await asyncpg.create_pool(dsn=settings.dsn, min_size=1, max_size=5)
        log.info("Database pool ready")
    except Exception as e:
        log.error("Cannot connect to database", error=str(e))
        print(f"\nERROR: Cannot connect to database.\n{e}")
        print("Make sure Docker is running:  docker compose up -d")
        sys.exit(1)

    # Advisory lock — prevent multiple simultaneous daemon instances
    lock_conn = await pool.acquire()
    acquired = await lock_conn.fetchval(
        "SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_KEY
    )
    if not acquired:
        log.error("Another daemon instance is already running (advisory lock held)")
        print("\nAnother heartbeat daemon is already running. Exiting.")
        await pool.release(lock_conn)
        await pool.close()
        sys.exit(1)
    log.info("Advisory lock acquired — this is the sole daemon instance")

    # Guard: enabled
    cfg = await _read_config(pool)
    if not cfg.get("enabled", True):
        log.info("Heartbeat disabled — exiting")
        print("\nHeartbeat is disabled. Re-enable via heartbeat_configure tool.")
        await pool.release(lock_conn)
        await pool.close()
        sys.exit(0)

    cycle_count = int(cfg.get("cycle_count", 0))
    frequency   = str(cfg.get("frequency", "4x_daily"))
    log.info("Daemon starting", cycle_count=cycle_count, frequency=frequency)
    print(f"  Cycle count: {cycle_count}   Frequency: {frequency}")
    print("  Press Ctrl+C to stop cleanly.\n")

    # Main loop
    while not _shutdown.is_set():
        now = datetime.now(timezone.utc)
        cfg = await _read_config(pool)
        frequency = str(cfg.get("frequency", "4x_daily"))

        # ── Minimum interval guard ─────────────────────────────────────────
        # Prevents burst-firing on restart when next_run is in the past.
        last_run_str = cfg.get("last_run")
        if last_run_str and last_run_str not in (None, "null"):
            try:
                last_run_dt = datetime.fromisoformat(
                    str(last_run_str).replace("Z", "+00:00")
                )
                elapsed = (now - last_run_dt).total_seconds()
                min_secs = _min_interval_seconds(frequency)
                if elapsed < min_secs:
                    # Too soon — sleep until min interval has passed
                    sleep_until = last_run_dt + timedelta(seconds=min_secs)
                    log.warning(
                        "Minimum interval not met — rescheduling",
                        elapsed_hours=round(elapsed / 3600, 2),
                        min_hours=round(min_secs / 3600, 2),
                    )
                    print(f"  Too soon since last cycle ({elapsed/3600:.1f}h elapsed, "
                          f"min {min_secs/3600:.1f}h). Sleeping.")
                    await _set_config(pool, "next_run", sleep_until.isoformat())
                    if not await _sleep_until(sleep_until):
                        break
                    continue
            except Exception:
                pass

        # ── Determine when to run ──────────────────────────────────────────
        next_run_str = cfg.get("next_run")
        if next_run_str and next_run_str not in (None, "null"):
            try:
                next_run = datetime.fromisoformat(
                    str(next_run_str).replace("Z", "+00:00")
                )
            except Exception:
                next_run = now  # parse failure → run now
        else:
            next_run = now  # first run → immediate

        if not await _sleep_until(next_run):
            break

        if _shutdown.is_set():
            break

        # Re-read after sleep — enabled may have changed
        cfg = await _read_config(pool)
        if not cfg.get("enabled", True):
            log.info("Heartbeat disabled mid-run — stopping")
            break
        frequency = str(cfg.get("frequency", "4x_daily"))

        # ── Write next_run BEFORE the cycle starts ─────────────────────────
        # If the daemon crashes mid-cycle, restart will see a future next_run
        # and will NOT immediately re-fire.
        scheduled_next = _next_run_from_now(frequency)
        await _set_config(pool, "next_run", scheduled_next.isoformat())

        # ── Run cycle ──────────────────────────────────────────────────────
        _in_cycle = True
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{ts}] Running cycle...")
        try:
            summary = await hb.run_cycle(pool)
            tasks = [t["task"] for t in summary.get("tasks_run", [])]
            diary = summary.get("diary_entry_id")
            print(f"  Cycle #{summary['cycle_number']} complete  "
                  f"tasks: {tasks}  "
                  f"energy: {summary['energy_before']}→{summary['energy_after']}  "
                  f"diary: {'yes' if diary else 'no'}")
            if summary.get("notes"):
                for note in summary["notes"]:
                    print(f"  note: {note}")
            print(f"  Next run: {scheduled_next.strftime('%Y-%m-%d %H:%M UTC')}")
        except Exception as e:
            log.error("Cycle failed", error=str(e))
            print(f"  Cycle error: {e}")
        finally:
            _in_cycle = False

    # Clean shutdown
    log.info("Heartbeat daemon shutting down")
    print("\nShutting down cleanly...")
    try:
        await pool.release(lock_conn)
    except Exception:
        pass
    await pool.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
