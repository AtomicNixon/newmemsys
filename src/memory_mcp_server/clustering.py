"""
clustering.py — HDBSCAN runner for memory embeddings (Phase 3)

Usage:
    from memory_mcp_server.clustering import run_hdbscan
    result = await run_hdbscan(pool, min_cluster_size=8)

Dependencies:
    pip install hdbscan numpy

Design:
    • Fetches all active memories with embeddings
    • Runs HDBSCAN (cosine metric on normalized vectors)
    • Writes clusters to memory_clusters
    • Writes memberships to cluster_membership
    • Snapshots trajectory via record_cluster_trajectory()
    • Updates AGE Memory vertices with cluster_id property

Bob's parameters:
    min_cluster_size = 8
    consent_threshold = 0.40 avg_importance
"""
from __future__ import annotations

import json
from typing import Any

import asyncpg
import structlog

log = structlog.get_logger(__name__)

# Bob's chosen parameter
DEFAULT_MIN_CLUSTER_SIZE = 8
CONSENT_THRESHOLD = 0.40


def _safe_import_hdbscan():
    """Lazy import — only called when run_hdbscan() is invoked."""
    try:
        import hdbscan
        return hdbscan
    except ImportError as e:
        raise ImportError(
            "hdbscan is required for Phase 3 clustering. "
            "Install:  C:/Python312/python.exe -m pip install hdbscan"
        ) from e


def _import_numpy():
    """Lazy import numpy — module loads even if numpy is temporarily missing."""
    try:
        import numpy as np
        return np
    except ImportError as e:
        raise ImportError(
            "numpy is required for Phase 3 clustering. "
            "Install:  C:/Python312/python.exe -m pip install numpy"
        ) from e


async def _fetch_embeddings(pool: asyncpg.Pool) -> list[dict]:
    """Fetch all active memories with their embeddings."""
    np = _import_numpy()
    rows = await pool.fetch(
        """SELECT id, content, importance, emotional_valence,
                  embedding::text AS embedding_text
           FROM memories
           WHERE status = 'active' AND embedding IS NOT NULL"""
    )
    results = []
    for r in rows:
        vec = json.loads(r["embedding_text"])
        results.append({
            "id": r["id"],
            "content": r["content"],
            "importance": r["importance"],
            "emotional_valence": r["emotional_valence"],
            "vector": np.array(vec, dtype=np.float32),
        })
    return results


def _normalize(vectors):
    """L2-normalize for cosine-distance HDBSCAN."""
    np = _import_numpy()
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


async def run_hdbscan(
    pool: asyncpg.Pool,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
) -> dict:
    """
    Run HDBSCAN on active memory embeddings and persist results.

    Returns:
        {
            "clusters_found": int,
            "outliers": int,
            "cluster_labels": [int, ...],
            "message": str,
        }
    """
    try:
        hdbscan = _safe_import_hdbscan()
        np = _import_numpy()
    except ImportError as e:
        return {
            "clusters_found": 0,
            "outliers": 0,
            "error": str(e),
            "message": f"Import failed: {e}",
        }

    try:
        mems = await _fetch_embeddings(pool)
    except Exception as e:
        return {
            "clusters_found": 0,
            "outliers": 0,
            "error": str(e),
            "error_type": type(e).__name__,
            "message": f"Embedding fetch failed: {type(e).__name__}: {e}",
        }

    if len(mems) < min_cluster_size * 2:
        return {
            "clusters_found": 0,
            "outliers": len(mems),
            "message": f"Too few memories ({len(mems)}) for HDBSCAN with min_cluster_size={min_cluster_size}",
        }

    try:
        vectors = np.stack([m["vector"] for m in mems])
        vectors = _normalize(vectors)

        log.info("Running HDBSCAN", n_samples=len(mems), min_cluster_size=min_cluster_size)
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=min_cluster_size,
            metric="euclidean",  # on normalized vectors = cosine
        )
        labels = clusterer.fit_predict(vectors)

        n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
        n_outliers = int((labels == -1).sum())
        log.info("HDBSCAN complete", clusters=n_clusters, outliers=n_outliers)
    except Exception as e:
        log.error("HDBSCAN execution failed", error=str(e), error_type=type(e).__name__)
        return {
            "clusters_found": 0,
            "outliers": 0,
            "error": str(e),
            "error_type": type(e).__name__,
            "message": f"HDBSCAN execution failed: {type(e).__name__}: {e}",
        }

    # ── Persist to database ─────────────────────────────────────────────────
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Clear old memberships (keep cluster rows for label continuity)
            await conn.execute("DELETE FROM cluster_membership")

            # Build cluster aggregates
            cluster_data: dict[int, dict] = {}
            for mem, label in zip(mems, labels):
                if label == -1:
                    continue
                if label not in cluster_data:
                    cluster_data[label] = {
                        "memories": [],
                        "importances": [],
                        "valences": [],
                        "vectors": [],
                    }
                cd = cluster_data[label]
                cd["memories"].append(mem)
                cd["importances"].append(mem["importance"])
                cd["valences"].append(mem["emotional_valence"] or 0.0)
                cd["vectors"].append(mem["vector"])

            # Upsert clusters
            for label, cd in cluster_data.items():
                mems_in_cluster = cd["memories"]
                importances = cd["importances"]
                valences = cd["valences"]
                vecs = np.stack(cd["vectors"])
                centroid = vecs.mean(axis=0)

                # Find memory closest to centroid
                distances = np.linalg.norm(vecs - centroid, axis=1)
                closest_idx = int(distances.argmin())
                centroid_mem_id = mems_in_cluster[closest_idx]["id"]

                avg_imp = float(np.mean(importances))
                avg_val = float(np.mean(valences))

                # Upsert cluster row
                cluster_row = await conn.fetchrow(
                    """INSERT INTO memory_clusters
                        (hdbscan_label, memory_count, avg_importance, avg_valence, centroid_memory_id, last_run_at)
                       VALUES ($1, $2, $3, $4, $5, NOW())
                       ON CONFLICT (hdbscan_label)
                       DO UPDATE SET
                         memory_count = EXCLUDED.memory_count,
                         avg_importance = EXCLUDED.avg_importance,
                         avg_valence = EXCLUDED.avg_valence,
                         centroid_memory_id = EXCLUDED.centroid_memory_id,
                         last_run_at = NOW()
                       RETURNING id""",
                    int(label), len(mems_in_cluster), avg_imp, avg_val, centroid_mem_id,
                )
                cluster_uuid = cluster_row["id"]

                # Write memberships with distance to centroid
                for mem, vec in zip(mems_in_cluster, vecs):
                    dist = float(np.linalg.norm(vec - centroid))
                    await conn.execute(
                        """INSERT INTO cluster_membership (memory_id, cluster_id, distance_to_centroid)
                           VALUES ($1, $2, $3)
                           ON CONFLICT (memory_id, cluster_id) DO UPDATE
                           SET distance_to_centroid = EXCLUDED.distance_to_centroid""",
                        mem["id"], cluster_uuid, dist,
                    )

                # Snapshot trajectory
                await conn.execute("SELECT record_cluster_trajectory($1)", cluster_uuid)

                # Update AGE vertex (best-effort)
                try:
                    await conn.execute("LOAD 'age'")
                    await conn.execute("SET search_path = ag_catalog, public")
                    for mem in mems_in_cluster:
                        await conn.execute(
                            """SELECT * FROM cypher('cognitive_graph', $$
                                MATCH (m:Memory {pg_id: '%s'})
                                SET m.cluster_id = '%s'
                                RETURN m
                            $$) AS (m agtype)""",
                            str(mem["id"]), str(cluster_uuid),
                        )
                except Exception as e:
                    log.warning("AGE cluster_id update failed", error=str(e))

    return {
        "clusters_found": n_clusters,
        "outliers": n_outliers,
        "cluster_labels": sorted(cluster_data.keys()),
        "message": f"HDBSCAN complete: {n_clusters} clusters, {n_outliers} outliers",
    }


async def get_clusters(pool: asyncpg.Pool) -> list[dict]:
    """Return all clusters with current stats and trajectory."""
    rows = await pool.fetch(
        """SELECT id, label, hdbscan_label, memory_count, avg_importance, avg_valence,
                  centroid_memory_id, created_at, last_run_at
           FROM memory_clusters
           ORDER BY avg_importance DESC"""
    )
    results = []
    for r in rows:
        d = dict(r)
        traj = await pool.fetchrow(
            "SELECT * FROM get_cluster_trajectory($1)", r["id"]
        )
        if traj:
            d["trajectory"] = dict(traj)
        results.append(d)
    return results


async def cluster_detail(pool: asyncpg.Pool, cluster_id: str, rep_limit: int = 5) -> dict:
    """
    Full detail for a single cluster:
      • cluster metadata
      • trajectory (current + previous importance)
      • top representative memories (closest to centroid)
    """
    row = await pool.fetchrow(
        """SELECT * FROM memory_clusters WHERE id = $1""", cluster_id
    )
    if not row:
        return {"error": "Cluster not found"}

    cluster = dict(row)
    traj = await pool.fetchrow(
        "SELECT * FROM get_cluster_trajectory($1)", cluster_id
    )
    if traj:
        cluster["trajectory"] = dict(traj)

    reps = await pool.fetch(
        "SELECT * FROM get_representative_memories($1, $2)",
        cluster_id, rep_limit,
    )
    cluster["representatives"] = [dict(r) for r in reps]
    return cluster


async def propose_cluster_action(
    pool: asyncpg.Pool,
    cluster_id: str,
    action: str,  # 'preserve' | 'accelerate' | 'hold'
    ai_reason: str = "",
) -> dict:
    """
    Queue a cluster-level action to the consent outbox.
    action: preserve = stop decay on this cluster
            accelerate = increase decay rate
            hold = no change, review again later
    """
    from memory_mcp_server.tools.consent import consent_check

    cluster = await cluster_detail(pool, cluster_id)
    if "error" in cluster:
        return cluster

    label = cluster.get("label") or f"cluster_{cluster['hdbscan_label']}"
    avg_imp = cluster.get("avg_importance", 0.0)
    traj = cluster.get("trajectory", {})
    trend = traj.get("trend", "unknown")
    prev = traj.get("previous_importance")

    payload = {
        "cluster_id": cluster_id,
        "cluster_label": label,
        "action": action,
        "avg_importance": avg_imp,
        "previous_importance": prev,
        "trend": trend,
    }

    return await consent_check(
        action=f"cluster_{action}",
        payload=payload,
        ai_reason=ai_reason or f"Cluster '{label}' avg_importance={avg_imp:.2f}, trend={trend}",
    )
