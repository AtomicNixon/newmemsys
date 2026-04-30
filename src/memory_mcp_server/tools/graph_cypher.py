"""
graph_cypher.py — MCP tools for Apache AGE graph traversals (Phase 2)

Each tool wraps a function from graph_age.py and returns clean dicts
suitable for MCP transport. All Cypher execution happens inside AGE's
cognitive_graph; connection initialisation (LOAD 'age', search_path) is
handled automatically by graph_age.py.
"""
from __future__ import annotations

from typing import Optional

import structlog

from memory_mcp_server import database as db
from memory_mcp_server import graph_age as age

log = structlog.get_logger(__name__)


async def find_causes_cypher(
    memory_id: str,
    depth: int = 5,
    fields: Optional[list[str]] = None,
) -> list[dict]:
    """
    Multi-hop causal chain via Cypher.
    Returns all Memory nodes reachable via CAUSES edges within depth hops,
    with hop count.

    fields: optional list of properties to return. Use ["pg_id","content","importance"]
            for slim payload on deep traversals with many results.
    """
    pool = await db.get_pool()
    results = await age.causal_chain(pool, memory_id, depth, fields)
    log.info("find_causes_cypher", memory_id=memory_id, depth=depth, fields=fields, found=len(results))
    return results


async def belief_support_cypher(topic: str) -> list[dict]:
    """
    Find memories that support a worldview belief.
    Returns memory nodes connected to the WorldView vertex via INFORMS_BELIEF edges.
    """
    pool = await db.get_pool()
    results = await age.belief_support(pool, topic)
    log.info("belief_support_cypher", topic=topic, found=len(results))
    return results


async def contradiction_cluster_cypher(memory_id: str) -> list[dict]:
    """
    Full contradiction neighbourhood via Cypher.
    Returns all memories in the CONTRADICTS subgraph reachable from this node
    (bidirectional, up to 3 hops).
    """
    pool = await db.get_pool()
    results = await age.contradiction_cluster(pool, memory_id)
    log.info("contradiction_cluster_cypher", memory_id=memory_id, found=len(results))
    return results


async def neighbourhood_cypher(memory_id: str, hops: int = 2) -> list[dict]:
    """
    All memories connected to this one within N hops, any edge type.
    Useful for: 'what else is near this memory in the graph?'
    Results sorted by distance (closest first).
    """
    pool = await db.get_pool()
    results = await age.neighbourhood(pool, memory_id, hops)
    log.info("neighbourhood_cypher", memory_id=memory_id, hops=hops, found=len(results))
    return results


async def path_between_cypher(id_a: str, id_b: str, max_hops: int = 6) -> Optional[dict]:
    """
    Shortest path between two memories.
    Returns path length and ordered list of intermediate memory pg_ids,
    or null if no path exists within max_hops.
    """
    pool = await db.get_pool()
    result = await age.path_between(pool, id_a, id_b, max_hops)
    log.info("path_between_cypher", id_a=id_a, id_b=id_b, found=result is not None)
    return result


async def age_graph_status() -> dict:
    """
    Quick status: vertex count, edge count, comparison with relational tables.
    """
    pool = await db.get_pool()
    stats = await age.age_stats(pool)
    return stats
