"""Health tool: system metrics."""
from __future__ import annotations

import json

from memory_mcp_server import database as db
from memory_mcp_server.embeddings import check_ollama


async def health() -> dict:
    """Return system health metrics."""
    row = await db.fetchrow("SELECT * FROM v_health")
    if row is None:
        return {"error": "health view unavailable"}

    metrics = dict(row)
    # Convert timedelta to string
    for k, v in metrics.items():
        if hasattr(v, "total_seconds"):
            metrics[k] = str(v)
        elif hasattr(v, "isoformat"):
            metrics[k] = v.isoformat()

    metrics["ollama_reachable"] = check_ollama()
    return metrics
