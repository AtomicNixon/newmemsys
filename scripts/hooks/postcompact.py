"""PostCompact lifecycle hook for NewMemSys.

Called by Claude Code after compaction fires. Reads the compact summary
from stdin, saves it as a session_summary memory, and queues a
postcompact_summary consent item so Bob can approve what gets permanently
indexed and linked into the graph/diary.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_HOOK_DIR))
from _mcp_client import call_tool, mcp_session, run  # noqa: E402


SUMMARY_MAX_CHARS = 12000  # keep the memory content bounded


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, max_len: int = SUMMARY_MAX_CHARS) -> str:
    text = text or ""
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


def _read_compact_summary() -> dict:
    """Read the compact summary JSON from stdin."""
    payload = sys.stdin.read()
    if not payload.strip():
        return {"raw": "", "sections": {}}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # Claude may send plain text; treat it as a single section.
        data = {"raw": payload, "sections": {"summary": payload}}

    if isinstance(data, str):
        data = {"raw": data, "sections": {"summary": data}}

    sections = data.get("sections") or {}
    if not sections and data:
        # Heuristic extraction from hub-style 9-section summaries.
        sections = _extract_sections(data.get("raw", ""))
    data.setdefault("sections", sections)
    data.setdefault("raw", payload)
    return data


def _extract_sections(text: str) -> dict[str, str]:
    """Best-effort parse of a sectioned plain-text compact summary."""
    sections: dict[str, str] = {}
    current_key = "summary"
    current_lines: list[str] = []
    # Match headers like "## Key Facts" or "Key Facts:" or "=== Key Facts ==="
    header_re = re.compile(r"^(?:#+\s*|={2,}\s*)?([A-Za-z][A-Za-z\s_]*?)(?:\s*:)?(?:\s*=+)?$")

    for line in text.splitlines():
        m = header_re.match(line.strip())
        if m:
            if current_lines:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = m.group(1).strip().lower().replace(" ", "_")
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections[current_key] = "\n".join(current_lines).strip()
    return sections


def _build_summary_text(summary: dict) -> str:
    sections = summary.get("sections") or {}
    lines = [f"Post-compact session summary at {_now()}"]
    for key, value in sections.items():
        value = value.strip()
        if not value:
            continue
        lines.append(f"\n## {key}\n")
        lines.append(_truncate(value, 2000))
    raw = summary.get("raw", "").strip()
    if raw and not sections:
        lines.append("\n## summary\n")
        lines.append(_truncate(raw))
    return "\n".join(lines)


def _extract_topic_hints(summary: dict) -> list[str]:
    """Pull likely WorldView topic keywords from the summary for later graph linking."""
    text = summary.get("raw", "")
    words = re.findall(r"[A-Za-z_]{4,}", text.lower())
    # Deduplicate preserving order.
    seen: set[str] = set()
    hints: list[str] = []
    for w in words:
        if w not in seen and len(w) >= 5:
            seen.add(w)
            hints.append(w)
    return hints[:20]


async def main():
    parser = argparse.ArgumentParser(description="NewMemSys PostCompact hook")
    parser.add_argument("--importance", type=float, default=0.3)
    args = parser.parse_args()

    summary = _read_compact_summary()
    content = _build_summary_text(summary)
    topic_hints = _extract_topic_hints(summary)

    async with mcp_session() as session:
        memory = await call_tool(
            session,
            "remember",
            content=content,
            type="session_summary",
            importance=args.importance,
            tags=["session_summary", "postcompact"],
            context={"source": "claude-code-postcompact", "topic_hints": topic_hints},
        )

        memory_id = None
        if isinstance(memory, dict):
            memory_id = memory.get("id")
        elif isinstance(memory, str):
            try:
                parsed = json.loads(memory)
                memory_id = parsed.get("id")
            except Exception:
                pass

        await call_tool(
            session,
            "consent_check",
            action="postcompact_summary",
            payload={
                "memory_id": memory_id,
                "topic_hints": topic_hints,
                "section_keys": list(summary.get("sections", {}).keys()),
                "summary_preview": content[:300],
            },
            ai_reason=(
                "Post-compact session summary saved as a memory. "
                "Approve to index it permanently and link to relevant WorldView beliefs; "
                "reject to archive it."
            ),
        )

    print(json.dumps({
        "memory_id": memory_id,
        "status": "queued",
        "topic_hints": topic_hints,
    }, indent=2))


if __name__ == "__main__":
    run(main())
