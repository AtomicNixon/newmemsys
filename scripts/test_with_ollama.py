"""
test_with_ollama.py — Test the memory system using qwen3.5 via Ollama.

Runs a short agentic loop:
  1. Sends a prompt to qwen3.5 via Ollama's OpenAI-compatible API
  2. When the model calls a memory tool, executes it against the real DB
  3. Feeds results back until the model produces a final answer

Usage:
    python scripts/test_with_ollama.py

Requires:
    - Docker container (newmemsys_brain) running on port 5433
    - Ollama running with qwen3.5:latest
    - Package installed: pip install -e .
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Load .env
env_path = ROOT / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = "qwen3.5:latest"
BOLD  = "\033[1m"
CYAN  = "\033[96m"
GREEN = "\033[92m"
YELLOW= "\033[93m"
RESET = "\033[0m"

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Store a new memory in the database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content":           {"type": "string"},
                    "type":              {"type": "string", "enum": ["episodic","semantic","procedural","strategic","working"], "default": "episodic"},
                    "importance":        {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5, "description": "Float 0.0 to 1.0 — never send integers greater than 1."},
                    "emotional_valence": {"type": "number", "minimum": -1.0, "maximum": 1.0, "default": 0.0},
                    "tags":              {"type": "array", "items": {"type": "string"}, "default": []},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Search memories semantically. Returns relevant stored memories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":          {"type": "string"},
                    "limit":          {"type": "integer", "default": 5},
                    "min_importance": {"type": "number", "default": 0.3},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_identity",
            "description": "Return Bob's core identity: name, purpose, loves, fears, commitments.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_diary",
            "description": "Write a prose diary entry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mood":  {"type": "string"},
                    "entry": {"type": "string"},
                },
                "required": ["mood", "entry"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_recent",
            "description": "Return the most recently stored memories.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 5}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "health",
            "description": "Return system health metrics: memory counts, DB status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# ---------------------------------------------------------------------------
# Tool executor — calls the real async memory functions
# ---------------------------------------------------------------------------

async def execute_tool(name: str, args: dict) -> str:
    from memory_mcp_server.tools import memory as mem
    from memory_mcp_server.tools import identity as id_tools
    from memory_mcp_server.tools import diary as diary_tools
    from memory_mcp_server.tools import health as health_tools

    try:
        match name:
            case "remember":        result = await mem.remember(**args)
            case "recall":          result = await mem.recall(**args)
            case "recall_recent":   result = await mem.recall_recent(**args)
            case "get_identity":    result = await id_tools.get_identity()
            case "write_diary":     result = await diary_tools.write_diary(**args)
            case "health":          result = await health_tools.health()
            case _:                 result = {"error": f"Unknown tool: {name}"}
        return json.dumps(result, default=str, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})

# ---------------------------------------------------------------------------
# Ollama chat call (OpenAI-compatible)
# ---------------------------------------------------------------------------

def chat(messages: list, tools: list | None = None) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.7},
    }
    if tools:
        payload["tools"] = tools

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())

# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

async def run_agent(user_prompt: str) -> None:
    from memory_mcp_server import database as db

    print(f"\n{BOLD}{CYAN}=== Memory System Test — qwen3.5:latest ==={RESET}\n")
    print(f"{BOLD}User:{RESET} {user_prompt}\n")

    # Init DB pool
    await db.get_pool()

    system_msg = (
        "You are Bob, an AI with a persistent memory system backed by PostgreSQL. "
        "You have access to memory tools: remember, recall, recall_recent, get_identity, "
        "write_diary, and health. "
        "When asked to test the system, use several tools to demonstrate that memory "
        "storage and retrieval are working. Be concise and show your results."
    )

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user",   "content": user_prompt},
    ]

    max_rounds = 10
    for round_num in range(max_rounds):
        print(f"{YELLOW}[Round {round_num + 1}] Calling {MODEL}...{RESET}")
        response = chat(messages, tools=TOOL_SCHEMAS)
        msg = response["choices"][0]["message"]
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            # Final answer
            print(f"\n{BOLD}{GREEN}Bob:{RESET} {msg.get('content', '').strip()}\n")
            break

        # Execute each tool call
        for tc in tool_calls:
            fn   = tc["function"]["name"]
            args = json.loads(tc["function"].get("arguments", "{}"))
            tc_id = tc.get("id", fn)

            print(f"  {CYAN}→ {fn}{RESET}({', '.join(f'{k}={repr(v)}' for k, v in args.items())})")
            result = await execute_tool(fn, args)
            result_preview = result[:200] + "..." if len(result) > 200 else result
            print(f"    {result_preview}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result,
            })
    else:
        print(f"{YELLOW}[Max rounds reached]{RESET}")

    await db.close_pool()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    prompt = (
        "Please test the memory system: "
        "1) Check your identity. "
        "2) Store a memory about this test session. "
        "3) Recall it back. "
        "4) Write a short diary entry about how the test went. "
        "5) Report the system health. "
        "Summarise what you found."
    )
    asyncio.run(run_agent(prompt))
