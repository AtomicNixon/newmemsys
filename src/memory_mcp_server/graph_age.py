"""
graph_age.py — Apache AGE Cypher query layer (Phase 2)

Every function in this module operates on the 'cognitive_graph' AGE graph.
AGE requires two things per connection:
  1. LOAD 'age'
  2. SET search_path = ag_catalog, "$user", public

Both are applied via _age_conn() before any Cypher is executed.

AGE returns results as the 'agtype' custom type, which is a superset of JSON.
asyncpg sees agtype values as plain strings; _parse_agtype() converts them
to Python dicts/lists. Simple scalar agtypes (integers, strings) are also
handled.

GRAPH STRUCTURE
  Vertices  : Memory, WorldView, Goal, Drive
  Edges     : CAUSES, CAUSED_BY, RELATED_TO, CONTRADICTS, SUPPORTS,
              PRECEDES, FOLLOWS, PART_OF, EXAMPLE_OF,
              INFORMS_BELIEF, DRIVES_GOAL
  Key prop  : pg_id (UUID string) — foreign key back to PostgreSQL tables

PHASE 2 QUERIES (implemented here)
  causal_chain(memory_id, depth)  — multi-hop cause traversal
  belief_support(topic)           — memories supporting a worldview entry
  contradiction_cluster(memory_id) — full contradiction neighbourhood
  neighbourhood(memory_id, hops)  — all connected memories within N hops
  path_between(id_a, id_b)        — shortest path between two memories

PHASE 3 HOOKS (stubs — will be filled during clustering work)
  cluster_neighbours(memory_id)   — memories in the same AGE cluster
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import asyncpg
import structlog

log = structlog.get_logger(__name__)

GRAPH = "cognitive_graph"


# =============================================================================
# Connection helpers
# =============================================================================

async def _age_conn(pool: asyncpg.Pool) -> asyncpg.Connection:
    """Acquire a connection and initialise it for AGE use."""
    conn = await pool.acquire()
    await conn.execute("LOAD 'age'")
    await conn.execute("SET search_path = ag_catalog, \"$user\", public")
    return conn


async def _release(pool: asyncpg.Pool, conn: asyncpg.Connection) -> None:
    try:
        await pool.release(conn)
    except Exception:
        pass


def _parse_agtype(raw: Any) -> Any:
    """Convert an agtype string returned by asyncpg into a Python object."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    # AGE adds type annotations like ::vertex, ::edge, ::path — strip them
    cleaned = re.sub(r"::(vertex|edge|path|agtype)$", "", raw.strip())
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return raw


def _extract_props(node: Any) -> dict:
    """Pull the 'properties' dict out of an AGE vertex/edge result."""
    if isinstance(node, dict):
        return node.get("properties", node)
    parsed = _parse_agtype(node)
    if isinstance(parsed, dict):
        return parsed.get("properties", parsed)
    return {}


# =============================================================================
# Raw Cypher execution
# =============================================================================

async def cypher_query(
    pool: asyncpg.Pool,
    query: str,
    columns: list[str],
    graph: str = GRAPH,
) -> list[dict]:
    """
    Execute a Cypher query and return rows as dicts.

    Args:
        pool:    asyncpg connection pool
        query:   Cypher query string (no leading/trailing $$)
        columns: list of return column names (must match RETURN clause)
        graph:   graph name (default: cognitive_graph)

    Example:
        await cypher_query(
            pool,
            "MATCH (m:Memory) WHERE m.importance > 0.8 RETURN m",
            ["m"],
        )
    """
    col_spec = ", ".join(f"{c} agtype" for c in columns)
    sql = f"SELECT * FROM cypher('{graph}', $$ {query} $$) AS ({col_spec})"

    conn = await _age_conn(pool)
    try:
        rows = await conn.fetch(sql)
        return [
            {col: _parse_agtype(row[col]) for col in columns}
            for row in rows
        ]
    except Exception as e:
        log.error("cypher_query failed", query=query[:120], error=str(e))
        raise
    finally:
        await _release(pool, conn)


# =============================================================================
# Phase 2 traversal queries
# =============================================================================

async def causal_chain(
    pool: asyncpg.Pool,
    memory_id: str,
    depth: int = 5,
    fields: Optional[list[str]] = None,
) -> list[dict]:
    """
    Multi-hop causal chain from a memory.
    Returns all Memory nodes reachable via CAUSES edges within `depth` hops,
    with hop count and path length.

    fields: optional list of properties to return. Omit for all.
            Use ["pg_id","content","importance"] for slim payload.
    """
    query = f"""
        MATCH path = (start:Memory {{pg_id: '{memory_id}'}})-[:CAUSES*1..{depth}]->(effect:Memory)
        RETURN effect, length(path) AS hops
    """
    rows = await cypher_query(pool, query, ["effect", "hops"])
    results = []
    for row in rows:
        props = _extract_props(row["effect"])
        props["hops"] = _parse_agtype(row["hops"])
        if fields:
            props = {k: v for k, v in props.items() if k in fields}
        results.append(props)
    return results


async def belief_support(
    pool: asyncpg.Pool,
    topic: str,
) -> list[dict]:
    """
    Find Memory nodes connected to a WorldView entry via INFORMS_BELIEF edges.
    Answers: 'what memories support this belief?'
    """
    query = f"""
        MATCH (m:Memory)-[:INFORMS_BELIEF]->(w:WorldView {{topic: '{topic}'}})
        RETURN m, w
    """
    rows = await cypher_query(pool, query, ["m", "w"])
    return [
        {
            "memory": _extract_props(row["m"]),
            "worldview": _extract_props(row["w"]),
        }
        for row in rows
    ]


async def contradiction_cluster(
    pool: asyncpg.Pool,
    memory_id: str,
) -> list[dict]:
    """
    Full contradiction neighbourhood: all memories in the contradiction
    subgraph reachable from this node (bidirectional CONTRADICTS traversal).
    """
    query = f"""
        MATCH (start:Memory {{pg_id: '{memory_id}'}})-[:CONTRADICTS*1..3]-(other:Memory)
        RETURN DISTINCT other
    """
    rows = await cypher_query(pool, query, ["other"])
    return [_extract_props(row["other"]) for row in rows]


async def neighbourhood(
    pool: asyncpg.Pool,
    memory_id: str,
    hops: int = 2,
) -> list[dict]:
    """
    All Memory nodes connected to this one within `hops` steps, any edge type.
    Useful for: 'what else is near this memory in the graph?'
    Returns: list of dicts with memory properties + relationship type + distance.
    """
    query = f"""
        MATCH (start:Memory {{pg_id: '{memory_id}'}})-[r*1..{hops}]-(neighbour:Memory)
        WHERE neighbour.pg_id <> '{memory_id}'
        RETURN DISTINCT neighbour, length(r) AS distance
    """
    rows = await cypher_query(pool, query, ["neighbour", "distance"])
    results = []
    for row in rows:
        props = _extract_props(row["neighbour"])
        props["distance"] = _parse_agtype(row["distance"])
        results.append(props)
    return sorted(results, key=lambda x: x.get("distance", 99))


async def path_between(
    pool: asyncpg.Pool,
    id_a: str,
    id_b: str,
    max_hops: int = 6,
) -> Optional[dict]:
    """
    Shortest path between two memories.
    Returns path length and the list of intermediate memory pg_ids,
    or None if no path exists within max_hops.
    """
    query = f"""
        MATCH path = shortestPath(
            (a:Memory {{pg_id: '{id_a}'}})-[*1..{max_hops}]-(b:Memory {{pg_id: '{id_b}'}})
        )
        RETURN path, length(path) AS hops
    """
    try:
        rows = await cypher_query(pool, query, ["path", "hops"])
    except Exception:
        return None

    if not rows:
        return None

    row = rows[0]
    hops = _parse_agtype(row["hops"])
    path_data = _parse_agtype(row["path"])

    # Extract vertex pg_ids from the path
    vertices = []
    if isinstance(path_data, dict) and "vertices" in path_data:
        for v in path_data["vertices"]:
            props = _extract_props(v)
            if "pg_id" in props:
                vertices.append(props["pg_id"])

    return {"hops": hops, "path": vertices, "from": id_a, "to": id_b}


# =============================================================================
# Sync helpers (called from heartbeat or manual migration)
# =============================================================================

async def sync_all(pool: asyncpg.Pool) -> dict:
    """
    Sync all active memories and graph edges from PostgreSQL into AGE.
    Calls the PL/pgSQL functions defined in 05_age_graph.sql.
    Safe to call multiple times — skips already-present vertices/edges.
    """
    conn = await _age_conn(pool)
    try:
        mem_row = await conn.fetchrow("SELECT * FROM sync_memories_to_age()")
        edge_row = await conn.fetchrow("SELECT * FROM sync_edges_to_age()")
        return {
            "memories": {"inserted": mem_row["inserted"], "skipped": mem_row["skipped"]},
            "edges":    {"inserted": edge_row["inserted"], "skipped": edge_row["skipped"]},
        }
    finally:
        await _release(pool, conn)


async def age_stats(pool: asyncpg.Pool) -> dict:
    """Return graph statistics from the age_graph_stats view."""
    conn = await _age_conn(pool)
    try:
        row = await conn.fetchrow("SELECT * FROM age_graph_stats")
        return dict(row) if row else {}
    finally:
        await _release(pool, conn)


# =============================================================================
# Phase 3 stubs (HDBSCAN clustering integration)
# =============================================================================

async def cluster_neighbours(
    pool: asyncpg.Pool,
    memory_id: str,
) -> list[dict]:
    """
    STUB — Phase 3.
    Return memories in the same HDBSCAN cluster as this memory.
    Will use cluster_id property on Memory vertices once clustering runs.
    """
    log.warning("cluster_neighbours: Phase 3 stub — clustering not yet implemented")
    return []
