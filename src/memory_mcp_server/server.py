"""MCP server — registers all tools and runs stdio transport."""
from __future__ import annotations

import json
import asyncio
import sys
import logging
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
import structlog

# ---------------------------------------------------------------------------
# Logging: MUST write to stderr only.
# stdout is reserved exclusively for MCP JSON-RPC framing.
# Any output to stdout breaks the protocol and causes JSON parse errors.
# ---------------------------------------------------------------------------
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

from memory_mcp_server import database as db
from memory_mcp_server.embeddings import check_ollama
from memory_mcp_server.tools import (
    memory as mem_tools,
    graph as graph_tools,
    graph_cypher as gc_tools,
    identity as id_tools,
    diary as diary_tools,
    consent as consent_tools,
    heartbeat as hb_tools,
    clustering as cl_tools,
)

log = structlog.get_logger(__name__)

app = Server("memory-system")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    types.Tool(
        name="memory",
        description=(
            "Unified memory dispatcher. Use 'action' to select the operation: "
            "remember, recall, recall_recent, hydrate, hydrate_light, remember_batch, "
            "remember_everywhere, edit, edit_batch, delete. "
            "Replaces the previous individual memory tools to reduce MCP schema size."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "remember", "recall", "recall_recent", "hydrate", "hydrate_light",
                        "remember_batch", "remember_everywhere", "edit", "edit_batch", "delete",
                        "flag_topic", "get_session_flags", "clear_session_flags",
                    ],
                    "default": "remember",
                    "description": "Memory operation to perform.",
                },
                # remember / remember_everywhere
                "content":           {"type": "string", "description": "Memory content (remember, remember_everywhere)."},
                "type":              {"type": "string", "enum": ["episodic","semantic","procedural","strategic","working","session_summary"], "default": "episodic"},
                "importance":        {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5},
                "emotional_valence": {"type": "number", "minimum": -1.0, "maximum": 1.0, "default": 0.0},
                "trust_level":       {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.8},
                "priority":          {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                "half_life_hours":   {"type": "integer", "default": 720},
                "tags":              {"type": "array", "items": {"type": "string"}, "default": []},
                "context":           {"type": "object", "default": {}},
                # recall / hydrate
                "query":             {"type": "string", "description": "Search query (recall, hydrate)."},
                "limit":             {"type": "integer", "default": 10},
                "min_importance":    {"type": "number", "default": 0.3},
                "max_importance":    {"type": "number", "default": 1.0},
                "memory_type":       {"type": "string", "enum": ["episodic","semantic","procedural","strategic","working","session_summary"]},
                "fields":            {"type": "array", "items": {"type": "string"}},
                "content_truncate":  {"type": "integer", "default": 0},
                "slim":              {"type": "boolean", "default": False},
                "brief":             {"type": "boolean", "default": False},
                # edit / delete
                "id":                {"type": "string", "description": "Memory UUID (edit, delete)."},
                "status":            {"type": "string", "enum": ["active","expired","archived","deleted"]},
                "hard":              {"type": "boolean", "default": False, "description": "Permanent delete (delete only)."},
                # batch operations
                "items":             {"type": "array", "items": {"type": "object"}, "description": "List of memory dicts (remember_batch, edit_batch)."},
                # session topic flags
                "session_id":        {"type": "string", "description": "Session ID for flag_topic/get_session_flags/clear_session_flags."},
                "note":              {"type": "string", "description": "Optional note for flag_topic."},
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="memory_graph",
        description=(
            "Unified memory graph dispatcher. Use 'mode' to select the operation: "
            "connect, connect_batch, disconnect, find_causes, find_contradictions, "
            "find_causes_cypher, belief_support_cypher, contradiction_cluster_cypher, "
            "neighbourhood_cypher, path_between_cypher, age_graph_status, sync_worldview, "
            "connect_belief. Replaces the previous individual graph tools."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": [
                        "connect", "connect_batch", "disconnect",
                        "find_causes", "find_contradictions",
                        "find_causes_cypher", "belief_support_cypher",
                        "contradiction_cluster_cypher", "neighbourhood_cypher",
                        "path_between_cypher", "age_graph_status",
                        "sync_worldview", "connect_belief",
                    ],
                    "description": "Graph operation to perform.",
                },
                # connect / connect_belief
                "from_id":           {"type": "string", "description": "Source memory UUID (connect) or memory_id (connect_belief)."},
                "memory_id":         {"type": "string"},
                "to_id":             {"type": "string", "description": "Target memory UUID (connect)."},
                "worldview_id":      {"type": "string", "description": "Target WorldView UUID (connect_belief)."},
                "relationship_type": {"type": "string", "default": "related_to"},
                "confidence":        {"type": "number", "default": 0.8},
                "context":           {"type": "string"},
                # batch / disconnect
                "edges":             {"type": "array", "items": {"type": "object"}, "description": "List of edge dicts (connect_batch, disconnect)."},
                # traversal params
                "id":                {"type": "string", "description": "Memory UUID for find_* tools."},
                "depth":             {"type": "integer", "default": 3},
                "hops":              {"type": "integer", "default": 2},
                "max_hops":          {"type": "integer", "default": 6},
                "topic":             {"type": "string", "description": "WorldView topic for belief_support_cypher."},
                "id_a":              {"type": "string", "description": "Start node for path_between_cypher."},
                "id_b":              {"type": "string", "description": "End node for path_between_cypher."},
                "fields":            {"type": "array", "items": {"type": "string"}, "description": "Slim field list for find_causes_cypher."},
            },
            "required": ["mode"],
        },
    ),
    # ── Phase 3: HDBSCAN clustering ───────────────────────────────────────────
    types.Tool(
        name="clustering",
        description=(
            "Unified clustering dispatcher. Use 'action' to select the operation: "
            "run, diagnostic, get, get_priority, detail, propose_action, assign_memories. "
            "Replaces the previous individual clustering tools."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "run", "diagnostic", "get", "get_priority",
                        "detail", "propose_action", "assign_memories",
                    ],
                    "default": "get",
                    "description": "Clustering operation to perform.",
                },
                "min_cluster_size": {"type": "integer", "default": 8},
                "cluster_id":       {"type": "string"},
                "rep_limit":        {"type": "integer", "default": 5},
                "memory_ids":       {"type": "array", "items": {"type": "string"}},
                "ai_reason":        {"type": "string"},
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="identity",
        description=(
            "Unified identity/worldview/drive/goal dispatcher. Use 'action' to select "
            "the operation: get_identity, get_worldview, set_worldview, get_drives, "
            "get_goals, set_identity. Replaces the previous individual identity tools."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "get_identity", "get_worldview", "set_worldview",
                        "get_drives", "get_goals", "set_identity",
                    ],
                    "default": "get_identity",
                },
                "key":           {"type": "string"},
                "value":         {"type": "object"},
                "priority":      {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                "topic":         {"type": "string"},
                "belief":        {"type": "string"},
                "confidence":    {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.7},
                "source":        {"type": "string"},
                "contradicts_id":{"type": "string"},
                "limit":         {"type": "integer"},
                "full_text":     {"type": "boolean", "default": False},
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="diary",
        description=(
            "Unified diary dispatcher. Use 'action' to select write or read. "
            "Replaces the previous write_diary and read_diary tools."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["write", "read"], "default": "read"},
                "mood":   {"type": "string"},
                "entry":  {"type": "string"},
                "date":   {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "limit":  {"type": "integer", "default": 5},
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="consent",
        description=(
            "Unified consent dispatcher. Use 'action' to select check, list, or resolve. "
            "Replaces the previous consent_check, list_pending_consent, and resolve_consent tools."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action":    {"type": "string", "enum": ["check", "list", "resolve"], "default": "list"},
                "payload":   {"type": "object"},
                "ai_reason": {"type": "string"},
                "outbox_id": {"type": "string"},
                "decision":  {"type": "string", "enum": ["approved", "rejected"]},
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="heartbeat",
        description=(
            "Unified heartbeat/health dispatcher. Use 'action' to select status, configure, "
            "pulse, diagnostic, or health. Replaces the previous individual heartbeat and health tools."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "configure", "pulse", "diagnostic", "health"],
                    "default": "diagnostic",
                },
                "enabled":       {"type": "boolean"},
                "frequency":     {"type": "string", "enum": ["hourly","2x_daily","4x_daily","daily"]},
                "energy_budget": {"type": "integer", "minimum": 1, "maximum": 100},
                "chat_model":    {"type": "string"},
            },
            "required": ["action"],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
    except Exception as e:
        log.error("Tool error", tool=name, error=str(e))
        result = {"error": str(e)}

    text = json.dumps(result, indent=2, default=str)
    log.debug("tool_response_size", tool=name, bytes=len(text))
    return [types.TextContent(type="text", text=text)]


async def _dispatch(name: str, args: dict) -> Any:
    match name:
        case "memory":             return await mem_tools.dispatch(**args)
        case "memory_graph":       return await graph_tools.dispatch(**args) if args.get("mode") in (
            "connect", "connect_batch", "disconnect", "find_causes", "find_contradictions"
        ) else await gc_tools.dispatch(**args)
        case "clustering":         return await cl_tools.dispatch(**args)
        case "identity":           return await id_tools.dispatch(**args)
        case "diary":              return await diary_tools.dispatch(**args)
        case "consent":            return await consent_tools.dispatch(**args)
        case "heartbeat":          return await hb_tools.dispatch(**args)
        case _:
            return {"error": f"Unknown tool: {name}"}


async def main() -> None:
    log.info("Memory MCP server starting")
    pool_ready = False

    try:
        # Init DB pool
        try:
            await db.get_pool()
            pool_ready = True
            log.info("Database pool ready")
        except Exception as e:
            log.warning("Database not yet available", error=str(e))

        # Check Ollama
        if not check_ollama():
            log.warning("Ollama not reachable — embeddings will be skipped until it is")

        async with mcp.server.stdio.stdio_server() as (reader, writer):
            log.info("stdio transport ready")
            try:
                await app.run(reader, writer, app.create_initialization_options())
            except (asyncio.CancelledError, KeyboardInterrupt):
                log.info("stdio transport ended — shutting down")
    finally:
        # Close DB pool cleanly so the OS process can actually exit
        # instead of being orphaned with live asyncpg worker tasks.
        if pool_ready:
            log.info("Closing database pool")
            try:
                await asyncio.wait_for(db.close_pool(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("Pool close timed out after 5s — forcing exit")
            except Exception as e:
                log.warning("Pool close error", error=str(e))
        log.info("Memory MCP server shut down")
