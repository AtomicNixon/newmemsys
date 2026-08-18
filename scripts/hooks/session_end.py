"""Session-end lifecycle hook for NewMemSys.

Called by Claude Code when a session ends without compaction firing.
Mirrors the PostCompact behavior: reads the session transcript context
from stdin, saves a session_summary memory, and queues a consent item
for Bob to approve/reject permanent indexing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_HOOK_DIR))
from _mcp_client import call_tool, mcp_session, run  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_context() -> dict:
    """Read optional session context from stdin."""
    if sys.stdin.isatty():
        return {}
    payload = sys.stdin.read().strip()
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except Exception:
        return {"raw_transcript": payload}


def _topic_hints(text: str) -> list[str]:
    import re
    words = re.findall(r"[A-Za-z_]{5,}", text.lower())
    seen: set[str] = set()
    hints: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            hints.append(w)
    return hints[:20]


async def main():
    parser = argparse.ArgumentParser(description="NewMemSys session-end hook")
    parser.add_argument("--importance", type=float, default=0.3)
    args = parser.parse_args()

    ctx = _read_context()
    raw = ctx.get("raw_transcript", "")
    summary_text = ctx.get("summary") or raw[:10000] or "Session ended. No transcript summary provided."
    topic_hints = _topic_hints(summary_text)

    content = (
        f"Cross-session handoff summary at {_now()}\n\n"
        f"{summary_text[:10000]}"
    )

    async with mcp_session() as session:
        memory = await call_tool(
            session,
            "memory",
            action="remember",
            content=content,
            type="session_summary",
            importance=args.importance,
            tags=["session_summary", "session_end"],
            context={"source": "claude-code-session-end", "topic_hints": topic_hints},
        )

        memory_id = None
        if isinstance(memory, dict):
            memory_id = memory.get("id")
        elif isinstance(memory, str):
            try:
                memory_id = json.loads(memory).get("id")
            except Exception:
                pass

        await call_tool(
            session,
            "consent",
            action="check",
            payload={
                "action": "postcompact_summary",
                "memory_id": memory_id,
                "topic_hints": topic_hints,
                "section_keys": ["session_end"],
                "summary_preview": content[:300],
            },
            ai_reason=(
                "Session-end summary saved as a memory. "
                "Approve to index it permanently and link to relevant WorldView beliefs; "
                "reject to archive it."
            ),
        )

    print(json.dumps({"memory_id": memory_id, "status": "queued"}, indent=2))


if __name__ == "__main__":
    run(main())
