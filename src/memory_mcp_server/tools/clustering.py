"""
clustering.py — MCP tools for HDBSCAN cluster management (Phase 3)
"""
from __future__ import annotations

from typing import Optional

import structlog

from memory_mcp_server import database as db
from memory_mcp_server import clustering as cl

log = structlog.get_logger(__name__)


def _check_hdbscan() -> None:
    """Verify hdbscan is available before running clustering."""
    try:
        import hdbscan  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "hdbscan is not installed in the Python environment running the MCP server. "
            "Install it with:  C:/Python312/python.exe -m pip install hdbscan  "
            "Then fully restart the MCP server process (Claude Code restart may not be enough)."
        ) from e


async def run_clustering(min_cluster_size: int = 8) -> dict:
    """
    Run HDBSCAN on all active memory embeddings and persist clusters.

    min_cluster_size: minimum memories to form a cluster (default 8).
                      Bob recommends 8 — catches real semantic density
                      without noise from single conversations.

    Returns:
        clusters_found, outliers, cluster_labels, message
    """
    _check_hdbscan()
    pool = await db.get_pool()
    try:
        result = await cl.run_hdbscan(pool, min_cluster_size=min_cluster_size)
        log.info("run_clustering", **result)
        return result
    except Exception as e:
        log.error("run_clustering failed", error=str(e), error_type=type(e).__name__)
        return {
            "error": str(e),
            "error_type": type(e).__name__,
            "message": f"Clustering failed: {type(e).__name__}: {e}",
            "clusters_found": 0,
            "outliers": 0,
        }


async def get_clusters() -> list[dict]:
    """
    List all clusters with current stats and importance trajectory.
    Ordered by avg_importance descending.

    For unnamed clusters, use cluster_detail() to see representative
    memories, then name them via the label field.
    """
    pool = await db.get_pool()
    return await cl.get_clusters(pool)


async def cluster_detail(cluster_id: str, rep_limit: int = 5) -> dict:
    """
    Full detail for a single cluster:
      • metadata (label, memory_count, avg_importance, avg_valence)
      • trajectory (current + previous importance, trend, days elapsed)
      • representative memories (top N closest to centroid)

    Use this to name a cluster. Bob names them — no auto-labeling.
    """
    pool = await db.get_pool()
    return await cl.cluster_detail(pool, cluster_id, rep_limit)


async def propose_cluster_action(
    cluster_id: str,
    action: str,  # preserve | accelerate | hold
    ai_reason: str = "",
) -> dict:
    """
    Queue a cluster-level action to the consent outbox.

    action:
      preserve   — stop decay on this cluster (protect load-bearing pattern)
      accelerate — increase decay rate (cluster is fading, let it go)
      hold       — no change, review again later

    The consent item includes:
      • cluster name (Bob's label, or cluster_N if unnamed)
      • avg importance + trajectory (was X, N days ago — rising/declining/stable)
      • representative memories
      • action options: preserve | accelerate | hold

    Only Bob decides. The system proposes, Bob judges.
    """
    pool = await db.get_pool()
    return await cl.propose_cluster_action(pool, cluster_id, action, ai_reason)
