"""PreCompact lifecycle hook for NewMemSys.

Called by Claude Code before compaction fires. Queries recent memories,
active drives, and active goals from NewMemSys, scores them by
importance × recency, and prints a ranked priority preservation list to
stdout. The compactor appends this as Additional Instructions.

No consent delay — this is just a hint to the compactor, not a permanent write.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow importing the shared MCP client without installing the package.
_HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_HOOK_DIR))
from _mcp_client import call_tool, mcp_session, run  # noqa: E402


MAX_MEMORIES = 20
MAX_DRIVES = 10
MAX_GOALS = 10
OUTPUT_LIMIT = 4000  # characters, to keep compact guidance cheap


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _hours_since(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (_now_utc() - dt).total_seconds() / 3600.0
    except Exception:
        return 168.0  # default to one week if parsing fails


def _score(item: dict) -> float:
    importance = item.get("importance") or item.get("priority") or 0.5
    # Drives/goals may store priority as a string label.
    if isinstance(importance, str):
        importance = {
            "critical": 0.95,
            "high": 0.75,
            "normal": 0.5,
            "low": 0.25,
        }.get(importance.lower(), 0.5)
    importance = float(importance)
    if importance <= 0:
        importance = 0.1
    hours = _hours_since(item.get("updated_at") or item.get("created_at") or "")
    # Recency weight: 2.0 within an hour, 1.0 at 24h, 0.3 beyond a week.
    if hours <= 1:
        recency = 2.0
    elif hours <= 24:
        recency = 1.5 - (hours - 1) / 46.0
    elif hours <= 168:
        recency = 1.0 - (hours - 24) / 288.0
    else:
        recency = 0.3
    return importance * recency


def _preview(text: str, max_len: int = 120) -> str:
    text = text or ""
    text = text.replace("\n", " ").strip()
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


def _read_hook_context() -> dict:
    """Read optional session context from stdin or env."""
    data = {}
    if not sys.stdin.isatty():
        try:
            payload = sys.stdin.read().strip()
            if payload:
                data = json.loads(payload)
        except Exception:
            pass
    # Env vars can supplement/override stdin.
    if os.environ.get("NEWMEMSYS_PRECOMPACT_FILES"):
        data.setdefault("files", []).extend(
            os.environ["NEWMEMSYS_PRECOMPACT_FILES"].split(",")
        )
    return data


async def main():
    parser = argparse.ArgumentParser(description="NewMemSys PreCompact hook")
    parser.add_argument("--max-memories", type=int, default=MAX_MEMORIES)
    parser.add_argument("--max-drives", type=int, default=MAX_DRIVES)
    parser.add_argument("--max-goals", type=int, default=MAX_GOALS)
    args = parser.parse_args()

    context = _read_hook_context()

    async with mcp_session() as session:
        memories = await call_tool(session, "memory", action="recall_recent", limit=args.max_memories)
        drives = await call_tool(session, "identity", action="get_drives")
        goals = await call_tool(session, "identity", action="get_goals")

    # Flatten results — MCP text payloads are JSON strings; recall_recent returns a list.
    if isinstance(memories, str):
        memories = json.loads(memories)
    if isinstance(drives, str):
        drives = json.loads(drives)
    if isinstance(goals, str):
        goals = json.loads(goals)

    memories = memories or []
    drives = drives or []
    goals = goals or []

    candidates = []
    for m in memories:
        if not isinstance(m, dict):
            continue
        candidates.append({
            "kind": "memory",
            "id": m.get("id"),
            "text": _preview(m.get("content", "")),
            "score": _score(m),
        })
    for d in drives:
        if not isinstance(d, dict):
            continue
        candidates.append({
            "kind": "drive",
            "id": d.get("concept") or d.get("name"),
            "text": _preview(d.get("description", "")),
            "score": _score(d),
        })
    for g in goals:
        if not isinstance(g, dict):
            continue
        candidates.append({
            "kind": "goal",
            "id": g.get("title") or g.get("name"),
            "text": _preview(g.get("description", "")),
            "score": _score(g),
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)

    lines = [
        "=== NEWMEMSYS PRECOMPACT PRESERVATION GUIDANCE ===",
        "",
        "The following items are ranked by importance × recency. Please preserve them in the compact summary if possible.",
        "",
    ]

    for i, c in enumerate(candidates[: args.max_memories], 1):
        lines.append(f"{i}. [{c['kind'].upper()}] {c['id']} (score={c['score']:.2f})")
        lines.append(f"   {c['text']}")
        lines.append("")

    if context.get("files"):
        lines.append("Active files mentioned in this session:")
        for f in context["files"][:20]:
            lines.append(f"  - {f}")
        lines.append("")

    output = "\n".join(lines)[:OUTPUT_LIMIT]
    print(output)


if __name__ == "__main__":
    run(main())
