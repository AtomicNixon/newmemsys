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


async def clustering_diagnostic() -> dict:
    """
    Quick diagnostic: check hdbscan import, count embeddable memories,
    and verify DB connectivity without running HDBSCAN.
    Use this if run_clustering() crashes mysteriously.
    """
    import sys
    diag = {
        "python_executable": sys.executable,
        "python_version": sys.version,
        "hdbscan_available": False,
        "hdbscan_error": None,
        "numpy_available": False,
        "numpy_error": None,
        "embeddable_memories": 0,
        "db_connected": False,
        "db_error": None,
    }

    # Check hdbscan
    try:
        import hdbscan
        diag["hdbscan_available"] = True
        diag["hdbscan_has_HDBSCAN"] = hasattr(hdbscan, "HDBSCAN")
    except Exception as e:
        diag["hdbscan_error"] = f"{type(e).__name__}: {e}"

    # Check numpy
    try:
        import numpy as np
        diag["numpy_available"] = True
        diag["numpy_version"] = np.__version__
    except Exception as e:
        diag["numpy_error"] = f"{type(e).__name__}: {e}"

    # Check DB
    try:
        pool = await db.get_pool()
        row = await pool.fetchrow(
            "SELECT COUNT(*) AS n FROM memories WHERE status='active' AND embedding IS NOT NULL"
        )
        diag["embeddable_memories"] = row["n"] if row else 0
        diag["db_connected"] = True
    except Exception as e:
        diag["db_error"] = f"{type(e).__name__}: {e}"

    return diag


async def get_clusters() -> list[dict]:
    """
    List all clusters with current stats and importance trajectory.
    Ordered by avg_importance descending.

    For unnamed clusters, use cluster_detail() to see representative
    memories, then name them via the label field.
    """
    pool = await db.get_pool()
    return await cl.get_clusters(pool)


async def get_clusters_priority() -> list[dict]:
    """
    Return clusters sorted by priority for naming.

    Priority order:
      1. Named clusters with declining trend (need review)
      2. Named clusters below 0.40 threshold (need action)
      3. Unnamed clusters with highest avg_importance (name these first)
      4. All others

    Returns slim dicts: cluster_id, label, avg_importance, trend, memory_count, unnamed_rank
    """
    pool = await db.get_pool()
    rows = await pool.fetch(
        """SELECT id, label, hdbscan_label, memory_count, avg_importance
           FROM memory_clusters
           ORDER BY avg_importance DESC"""
    )
    results = []
    unnamed_rank = 0
    for r in rows:
        d = dict(r)
        traj = await pool.fetchrow(
            "SELECT * FROM get_cluster_trajectory($1)", r["id"]
        )
        trend = traj["trend"] if traj else "unknown"
        prev = traj["previous_importance"] if traj else None

        # Priority scoring
        priority = 0
        if d.get("label") and trend == "declining":
            priority = 1
        elif d.get("label") and d["avg_importance"] < 0.40:
            priority = 2
        elif not d.get("label"):
            unnamed_rank += 1
            priority = 3
        else:
            priority = 4

        results.append({
            "cluster_id": str(d["id"]),
            "label": d.get("label") or f"cluster_{d['hdbscan_label']}",
            "avg_importance": round(d["avg_importance"], 3),
            "trend": trend,
            "previous_importance": round(prev, 3) if prev else None,
            "memory_count": d["memory_count"],
            "unnamed_rank": unnamed_rank if not d.get("label") else None,
            "priority": priority,
        })

    results.sort(key=lambda x: (x["priority"], -x["avg_importance"]))
    return results


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


async def assign_memories_to_cluster(
    cluster_id: str,
    memory_ids: list[str],
) -> dict:
    """
    Bulk-assign multiple memories to a cluster in one call.

    This is the batch operation for the drawer→room workflow:
    instead of updating one memory at a time, pass a list of
    memory IDs and assign them all to the same cluster.

    Returns: assigned count, skipped count (already in cluster),
             cluster label, cluster memory count after assignment.
    """
    pool = await db.get_pool()

    # Verify cluster exists
    cluster = await pool.fetchrow(
        "SELECT id, label, memory_count FROM memory_clusters WHERE id = $1",
        cluster_id,
    )
    if not cluster:
        return {"error": f"Cluster {cluster_id} not found"}

    assigned = 0
    skipped = 0
    for mem_id in memory_ids:
        # Check memory exists and is active
        mem = await pool.fetchrow(
            "SELECT id FROM memories WHERE id = $1 AND status = 'active'",
            mem_id,
        )
        if not mem:
            continue

        # Upsert membership
        result = await pool.execute(
            """INSERT INTO cluster_membership (memory_id, cluster_id, distance_to_centroid)
               VALUES ($1, $2, NULL)
               ON CONFLICT (memory_id, cluster_id) DO NOTHING""",
            mem_id, cluster_id,
        )
        if result == "INSERT 0 0":
            skipped += 1
        else:
            assigned += 1

    # Update cluster memory count
    new_count = await pool.fetchval(
        "SELECT COUNT(*) FROM cluster_membership WHERE cluster_id = $1",
        cluster_id,
    )
    await pool.execute(
        "UPDATE memory_clusters SET memory_count = $1 WHERE id = $2",
        new_count, cluster_id,
    )

    return {
        "cluster_id": cluster_id,
        "cluster_label": cluster["label"] or f"cluster_{cluster_id[:8]}",
        "assigned": assigned,
        "skipped": skipped,
        "total_memories": new_count,
    }
