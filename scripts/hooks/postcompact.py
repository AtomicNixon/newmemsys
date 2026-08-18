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
    """Read the compact summary from stdin.

    Claude Code pipes hook-invocation JSON (session_id, transcript_path,
    hook_event_name, trigger, ...) to stdin, NOT the actual compaction
    summary text. The real summary lives in the transcript file or in a
    dedicated field. We try multiple extraction strategies.
    """
    payload = sys.stdin.read()
    if not payload.strip():
        return {"raw": "", "sections": {}}

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # Plain text — treat it as a single section.
        data = {"raw": payload, "sections": {"summary": payload}}

    if isinstance(data, str):
        data = {"raw": data, "sections": {"summary": data}}

    # If this looks like hook-invocation metadata, try to find the real summary.
    hook_fields = {"session_id", "transcript_path", "hook_event_name", "trigger", "prompt_id", "users"}
    if isinstance(data, dict) and hook_fields.intersection(data.keys()):
        # Strategy 1: look for a nested summary field.
        for key in ("summary", "compact_summary", "additional_instructions", "context"):
            if key in data and isinstance(data[key], str) and len(data[key]) > 50:
                data = {"raw": data[key], "sections": {"summary": data[key]}}
                break
        else:
            # Strategy 2: read the transcript file and extract the last assistant turn.
            transcript_path = data.get("transcript_path")
            if transcript_path and os.path.isfile(transcript_path):
                try:
                    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    # Walk backwards for the last assistant text block.
                    for line in reversed(lines):
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue
                        if entry.get("type") == "assistant":
                            content = entry.get("message", {}).get("content", [])
                            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                            text = "\n".join(t for t in texts if t).strip()
                            if text:
                                data = {"raw": text, "sections": {"summary": text}}
                                break
                except Exception:
                    pass

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
            "memory",
            action="remember",
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
            "consent",
            action="check",
            payload={
                "action": "postcompact_summary",
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
