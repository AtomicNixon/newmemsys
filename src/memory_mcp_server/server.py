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
    health as health_tools,
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
        name="remember",
        description="Store a new memory. Generates an embedding via Ollama.",
        inputSchema={
            "type": "object",
            "properties": {
                "content":           {"type": "string", "description": "The memory content."},
                "type":              {"type": "string", "enum": ["episodic","semantic","procedural","strategic","working"], "default": "episodic"},
                "importance":        {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5, "description": "Float 0.0 (trivial) to 1.0 (critical). Never send integers > 1."},
                "emotional_valence": {"type": "number", "minimum": -1.0, "maximum": 1.0, "default": 0.0, "description": "Float -1.0 (negative) to 1.0 (positive)."},
                "trust_level":       {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.8, "description": "Float 0.0 (untrusted) to 1.0 (fully trusted)."},
                "priority":          {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                "half_life_hours":   {"type": "integer", "default": 720},
                "tags":              {"type": "array", "items": {"type": "string"}, "default": []},
                "context":           {"type": "object", "default": {}},
            },
            "required": ["content"],
        },
    ),
    types.Tool(
        name="recall",
        description="Semantic memory search using vector similarity + full-text fallback.",
        inputSchema={
            "type": "object",
            "properties": {
                "query":          {"type": "string"},
                "limit":          {"type": "integer", "default": 10},
                "min_importance": {"type": "number", "default": 0.3},
                "max_importance": {"type": "number", "default": 1.0, "description": "Upper bound on importance (0.0–1.0). Use with min_importance=0 to surface only low-importance memories."},
                "memory_type":    {"type": "string", "enum": ["episodic","semantic","procedural","strategic","working"]},
                "fields":         {"type": "array", "items": {"type": "string"}, "description": "Columns to return. Omit for all. Use [\"id\",\"content\",\"importance\",\"emotional_valence\"] for slim payload during bulk sweeps."},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="recall_recent",
        description="Return the most recently created active memories.",
        inputSchema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    ),
    types.Tool(
        name="hydrate",
        description=(
            "Full cognitive context reconstruction: identity + worldview + diary + top memories. "
            "slim=True returns a token-economy version (keys only, no full text) — "
            "use for short sessions where orientation is enough. "
            "Saves 60–80% tokens vs full hydrate."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "slim":  {"type": "boolean", "default": False, "description": "Return slim payload: identity keys only, worldview topics+confidence only, diary date+mood only, memory id+importance only."},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="hydrate_light",
        description=(
            "Lightweight session start: identity keys + last 2 diary entries only. "
            "Use instead of hydrate() for short sessions or quick lookups where full "
            "context reconstruction is not needed. Significantly fewer tokens than hydrate()."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="remember_batch",
        description="Bulk-insert multiple memories.",
        inputSchema={
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}}
            },
            "required": ["items"],
        },
    ),
    types.Tool(
        name="edit",
        description=(
            "Partial update a memory. Only supplied fields change — everything else is preserved. "
            "created_at is never touched. If content changes, the embedding is regenerated. "
            "All fields except id are optional."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id":               {"type": "string", "description": "UUID of the memory to edit."},
                "content":          {"type": "string"},
                "importance":       {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "Float 0.0–1.0"},
                "emotional_valence":{"type": "number", "minimum": -1.0, "maximum": 1.0, "description": "Float -1.0–1.0"},
                "trust_level":      {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "half_life_hours":  {"type": "integer"},
                "tags":             {"type": "array", "items": {"type": "string"}},
                "status":           {"type": "string", "enum": ["active","expired","archived","deleted"]},
            },
            "required": ["id"],
        },
    ),
    types.Tool(
        name="edit_batch",
        description=(
            "Bulk partial-update multiple memories in one call. "
            "Each item must have 'id' plus any fields to change: "
            "importance, emotional_valence, trust_level, half_life_hours, tags, status. "
            "Ideal for valence/importance sweeps — fix 20-50 memories in one round-trip."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":                {"type": "string"},
                            "importance":        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                            "emotional_valence": {"type": "number", "minimum": -1.0, "maximum": 1.0},
                            "trust_level":       {"type": "number", "minimum": 0.0, "maximum": 1.0},
                            "half_life_hours":   {"type": "integer"},
                            "tags":              {"type": "array", "items": {"type": "string"}},
                            "status":            {"type": "string"},
                        },
                        "required": ["id"],
                    },
                }
            },
            "required": ["items"],
        },
    ),
    types.Tool(
        name="delete",
        description=(
            "Delete a memory. Default is soft delete (status='deleted', row preserved). "
            "Pass hard=true for permanent removal — use consent_check first for hard deletes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id":   {"type": "string", "description": "UUID of the memory to delete."},
                "hard": {"type": "boolean", "default": False, "description": "True = permanent. False (default) = soft delete."},
            },
            "required": ["id"],
        },
    ),
    types.Tool(
        name="connect",
        description="Create a human-curated edge between two memories (no auto-edges).",
        inputSchema={
            "type": "object",
            "properties": {
                "from_id":           {"type": "string"},
                "to_id":             {"type": "string"},
                "relationship_type": {"type": "string", "default": "related_to"},
                "confidence":        {"type": "number", "default": 0.8},
                "context":           {"type": "string"},
            },
            "required": ["from_id", "to_id"],
        },
    ),
    types.Tool(
        name="connect_batch",
        description="Bulk-create edges between memories.",
        inputSchema={
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object"}}
            },
            "required": ["items"],
        },
    ),
    types.Tool(
        name="find_causes",
        description="Recursive causal chain from a memory (memory_graph traversal).",
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "depth":     {"type": "integer", "default": 3},
            },
            "required": ["memory_id"],
        },
    ),
    types.Tool(
        name="find_contradictions",
        description="Find memories that contradict the given memory.",
        inputSchema={
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    ),
    # ── Phase 2: AGE Cypher graph traversals ──────────────────────────────────
    types.Tool(
        name="find_causes_cypher",
        description=(
            "Multi-hop causal chain via Cypher graph traversal (Phase 2). "
            "Returns all Memory nodes reachable via CAUSES edges within depth hops, "
            "with hop count. Replaces the recursive SQL find_causes once AGE is active."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "depth":     {"type": "integer", "default": 5},
                "fields":    {"type": "array", "items": {"type": "string"}, "description": "Properties to return. Use [\"pg_id\",\"content\",\"importance\"] for slim payload on deep traversals."},
            },
            "required": ["memory_id"],
        },
    ),
    types.Tool(
        name="belief_support_cypher",
        description=(
            "Find memories connected to a worldview belief via INFORMS_BELIEF edges. "
            "Answers: 'what memories support this belief?' Returns memory nodes "
            "and the worldview entry they connect to."
        ),
        inputSchema={
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    ),
    types.Tool(
        name="contradiction_cluster_cypher",
        description=(
            "Full contradiction neighbourhood via Cypher. "
            "Returns all memories in the CONTRADICTS subgraph reachable from this node "
            "(bidirectional, up to 3 hops)."
        ),
        inputSchema={
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    ),
    types.Tool(
        name="neighbourhood_cypher",
        description=(
            "All memories connected to this one within N hops, any edge type. "
            "Useful for: 'what else is near this memory in the graph?' "
            "Results sorted by distance (closest first)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "hops":      {"type": "integer", "default": 2},
            },
            "required": ["memory_id"],
        },
    ),
    types.Tool(
        name="path_between_cypher",
        description=(
            "Shortest path between two memories via Cypher. "
            "Returns path length and ordered list of intermediate memory pg_ids, "
            "or null if no path exists within max_hops."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id_a":      {"type": "string"},
                "id_b":      {"type": "string"},
                "max_hops":  {"type": "integer", "default": 6},
            },
            "required": ["id_a", "id_b"],
        },
    ),
    types.Tool(
        name="age_graph_status",
        description=(
            "Quick status of the AGE cognitive graph: vertex count, edge count, "
            "and comparison with the relational tables."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="connect_belief",
        description=(
            "Create an INFORMS_BELIEF edge from a Memory vertex to a WorldView vertex "
            "in the AGE graph. Idempotent — returns success=True if already connected. "
            "Use after sync_worldview_to_age() to wire memories to beliefs."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "memory_id":    {"type": "string", "description": "UUID of the memory (source)"},
                "worldview_id": {"type": "string", "description": "UUID of the worldview entry (target)"},
                "confidence":   {"type": "number", "default": 0.8},
                "context":      {"type": "string"},
            },
            "required": ["memory_id", "worldview_id"],
        },
    ),
    # ── Phase 3: HDBSCAN clustering ───────────────────────────────────────────
    types.Tool(
        name="run_clustering",
        description=(
            "Run HDBSCAN on all active memory embeddings and persist clusters. "
            "min_cluster_size=8 is Bob's recommended default. "
            "Creates cluster rows, memberships, trajectory snapshots, and "
            "updates AGE vertices with cluster_id. Safe to re-run — upserts in place."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "min_cluster_size": {"type": "integer", "default": 8},
            },
        },
    ),
    types.Tool(
        name="get_clusters",
        description=(
            "List all HDBSCAN clusters with current stats and importance trajectory. "
            "Ordered by avg_importance descending. "
            "For unnamed clusters, use cluster_detail() to see representative memories, "
            "then name them yourself — no auto-labeling."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="get_clusters_priority",
        description=(
            "Return clusters sorted by naming priority. "
            "Priority: declining named clusters → below-threshold named → "
            "highest-avg unnamed → all others. "
            "Use this to decide which cluster to name next."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="cluster_detail",
        description=(
            "Full detail for a single cluster: metadata, trajectory, and "
            "representative memories (top N closest to centroid). "
            "Use this to name a cluster. Bob names them — no auto-labeling."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "rep_limit":  {"type": "integer", "default": 5, "description": "Number of representative memories to return"},
            },
            "required": ["cluster_id"],
        },
    ),
    types.Tool(
        name="propose_cluster_action",
        description=(
            "Queue a cluster-level action to the consent outbox. "
            "action: preserve (stop decay) | accelerate (increase decay) | hold (no change). "
            "The consent item includes cluster name, avg importance + trajectory, "
            "representative memories, and action options. Only Bob decides."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "action":     {"type": "string", "enum": ["preserve", "accelerate", "hold"]},
                "ai_reason":  {"type": "string"},
            },
            "required": ["cluster_id", "action"],
        },
    ),
    types.Tool(
        name="assign_memories_to_cluster",
        description=(
            "Bulk-assign multiple memories to a cluster in one call. "
            "Pass a list of memory_ids and a cluster_id — all memories are "
            "assigned at once. Use this for the drawer→room workflow instead "
            "of updating memories one at a time."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_id":  {"type": "string", "description": "UUID of the target cluster"},
                "memory_ids":  {"type": "array", "items": {"type": "string"}, "description": "List of memory UUIDs to assign"},
            },
            "required": ["cluster_id", "memory_ids"],
        },
    ),
    types.Tool(
        name="clustering_diagnostic",
        description=(
            "Diagnostic tool for clustering issues. Checks hdbscan import, "
            "numpy version, DB connectivity, and embeddable memory count — "
            "without running HDBSCAN. Use this if run_clustering() crashes."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # ── end Phase 3 clustering tools ──────────────────────────────────────────
    types.Tool(
        name="get_identity",
        description="Return all identity keys ordered by priority.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="get_worldview",
        description=(
            "Return worldview beliefs ordered by confidence. "
            "limit=N returns only top N beliefs. "
            "full_text=True returns complete belief statements; default truncates to 200 chars "
            "and omits source to save tokens. "
            "Each entry includes its id (UUID) — use as worldview_id in connect_belief()."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit":     {"type": "integer", "description": "Return only top N beliefs by confidence"},
                "full_text": {"type": "boolean", "default": False, "description": "Return complete belief text and source. Default truncates to save tokens."},
            },
        },
    ),
    types.Tool(
        name="get_drives",
        description="Return currently active (non-expired) drives.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="get_goals",
        description="Return active goals.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="set_identity",
        description="Upsert an identity key.",
        inputSchema={
            "type": "object",
            "properties": {
                "key":      {"type": "string"},
                "value":    {"type": "object"},
                "priority": {"type": "integer", "default": 5},
            },
            "required": ["key", "value"],
        },
    ),
    types.Tool(
        name="set_worldview",
        description=(
            "Upsert a worldview belief. Worldview holds load-bearing beliefs — frameworks, "
            "principles, uncertainty anchors — not episodic memories. "
            "Upserts on topic (same topic = update in place). "
            "Supply contradicts_id to wire a symmetric contradiction link to another belief."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic":          {"type": "string", "description": "Short label, e.g. 'pattern_identity'."},
                "belief":         {"type": "string", "description": "Full statement of the belief."},
                "confidence":     {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.7},
                "source":         {"type": "string"},
                "contradicts_id": {"type": "string", "description": "UUID of a belief this one contradicts."},
            },
            "required": ["topic", "belief"],
        },
    ),
    types.Tool(
        name="write_diary",
        description="Write a diary prose entry.",
        inputSchema={
            "type": "object",
            "properties": {
                "mood":  {"type": "string"},
                "entry": {"type": "string"},
                "date":  {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
            },
            "required": ["mood", "entry"],
        },
    ),
    types.Tool(
        name="read_diary",
        description="Read recent diary entries.",
        inputSchema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 5}},
        },
    ),
    types.Tool(
        name="consent_check",
        description="Log a proposed memory action for human review. AI can refuse with a reason.",
        inputSchema={
            "type": "object",
            "properties": {
                "action":    {"type": "string"},
                "payload":   {"type": "object"},
                "ai_reason": {"type": "string"},
            },
            "required": ["action", "payload", "ai_reason"],
        },
    ),
    types.Tool(
        name="list_pending_consent",
        description="List all consent items awaiting human decision.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="resolve_consent",
        description="Approve or reject a pending consent item.",
        inputSchema={
            "type": "object",
            "properties": {
                "outbox_id": {"type": "string"},
                "decision":  {"type": "string", "enum": ["approved", "rejected"]},
            },
            "required": ["outbox_id", "decision"],
        },
    ),
    types.Tool(
        name="health",
        description="System health metrics: memory counts, Ollama status, DB stats.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="heartbeat_status",
        description="Return heartbeat daemon status: config, energy level, recent cycle logs.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="heartbeat_configure",
        description=(
            "Configure the heartbeat daemon. All fields optional — only supplied fields change. "
            "frequency: 'hourly' | '2x_daily' | '4x_daily' | 'daily'. "
            "energy_budget: 1–100 (refills every hour up to this cap). "
            "chat_model: Ollama model name for diary generation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "enabled":       {"type": "boolean"},
                "frequency":     {"type": "string", "enum": ["hourly","2x_daily","4x_daily","daily"]},
                "energy_budget": {"type": "integer", "minimum": 1, "maximum": 100},
                "chat_model":    {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="heartbeat_pulse",
        description=(
            "Trigger one heartbeat cycle immediately, regardless of schedule. "
            "Runs all tasks (maintenance, decay, drive monitor, contradictions, diary) "
            "within the current energy budget. Returns full cycle summary."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="heartbeat_diagnostic",
        description=(
            "Combined heartbeat status + system health in one call. "
            "Returns heartbeat config, energy level, recent cycles, and DB/Ollama health metrics. "
            "Use this at session startup instead of calling heartbeat_status and health separately."
        ),
        inputSchema={"type": "object", "properties": {}},
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

    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def _dispatch(name: str, args: dict) -> Any:
    match name:
        case "remember":           return await mem_tools.remember(**args)
        case "recall":             return await mem_tools.recall(**args)
        case "recall_recent":      return await mem_tools.recall_recent(**args)
        case "hydrate":            return await mem_tools.hydrate(**args)
        case "hydrate_light":      return await mem_tools.hydrate_light()
        case "remember_batch":     return await mem_tools.remember_batch(**args)
        case "edit":               return await mem_tools.edit(**args)
        case "edit_batch":         return await mem_tools.edit_batch(**args)
        case "delete":             return await mem_tools.delete(**args)
        case "connect":            return await graph_tools.connect(**args)
        case "find_causes":        return await graph_tools.find_causes(**args)
        case "find_contradictions":return await graph_tools.find_contradictions(**args)
        case "find_causes_cypher":      return await gc_tools.find_causes_cypher(**args)
        case "belief_support_cypher":   return await gc_tools.belief_support_cypher(**args)
        case "contradiction_cluster_cypher": return await gc_tools.contradiction_cluster_cypher(**args)
        case "neighbourhood_cypher":    return await gc_tools.neighbourhood_cypher(**args)
        case "path_between_cypher":     return await gc_tools.path_between_cypher(**args)
        case "age_graph_status":        return await gc_tools.age_graph_status()
        case "connect_belief":          return await gc_tools.connect_belief(**args)
        case "run_clustering":          return await cl_tools.run_clustering(**args)
        case "get_clusters":            return await cl_tools.get_clusters()
        case "get_clusters_priority":   return await cl_tools.get_clusters_priority()
        case "cluster_detail":          return await cl_tools.cluster_detail(**args)
        case "propose_cluster_action":  return await cl_tools.propose_cluster_action(**args)
        case "assign_memories_to_cluster": return await cl_tools.assign_memories_to_cluster(**args)
        case "clustering_diagnostic":   return await cl_tools.clustering_diagnostic()
        case "connect_batch":      return await graph_tools.connect_batch(**args)
        case "get_identity":       return await id_tools.get_identity()
        case "get_worldview":      return await id_tools.get_worldview()
        case "get_drives":         return await id_tools.get_drives()
        case "get_goals":          return await id_tools.get_goals()
        case "set_identity":       return await id_tools.set_identity(**args)
        case "set_worldview":      return await id_tools.set_worldview(**args)
        case "write_diary":        return await diary_tools.write_diary(**args)
        case "read_diary":         return await diary_tools.read_diary(**args)
        case "consent_check":      return await consent_tools.consent_check(**args)
        case "list_pending_consent": return await consent_tools.list_pending_consent()
        case "resolve_consent":    return await consent_tools.resolve_consent(**args)
        case "health":             return await health_tools.health()
        case "heartbeat_status":   return await hb_tools.heartbeat_status()
        case "heartbeat_configure":return await hb_tools.heartbeat_configure(**args)
        case "heartbeat_pulse":    return await hb_tools.heartbeat_pulse()
        case "heartbeat_diagnostic": return await hb_tools.heartbeat_diagnostic()
        case _:
            return {"error": f"Unknown tool: {name}"}


async def main() -> None:
    log.info("Memory MCP server starting")

    # Init DB pool
    try:
        await db.get_pool()
        log.info("Database pool ready")
    except Exception as e:
        log.warning("Database not yet available", error=str(e))

    # Check Ollama
    if not check_ollama():
        log.warning("Ollama not reachable — embeddings will be skipped until it is")

    async with mcp.server.stdio.stdio_server() as (reader, writer):
        log.info("stdio transport ready")
        await app.run(reader, writer, app.create_initialization_options())
