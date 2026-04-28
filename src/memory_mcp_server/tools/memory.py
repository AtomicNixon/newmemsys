"""Memory tools: remember, recall, recall_recent, hydrate, remember_batch, edit, delete."""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional, Annotated

import structlog

from memory_mcp_server import database as db
from memory_mcp_server.embeddings import embed

log = structlog.get_logger(__name__)


async def remember(
    content: str,
    type: str = "episodic",
    importance: float = 0.5,
    emotional_valence: float = 0.0,
    trust_level: float = 0.8,
    priority: int = 5,
    half_life_hours: int = 720,
    tags: Optional[list] = None,
    context: Optional[dict] = None,
) -> dict:
    """Store a memory, generating an embedding if possible."""
    tags = tags or []
    context = context or {}

    embedding = embed(content)

    sql = """
        INSERT INTO memories
          (type, content, embedding, importance, emotional_valence,
           trust_level, priority, half_life_hours, tags, context)
        VALUES ($1, $2, $3::vector, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb)
        RETURNING id, created_at
    """
    row = await db.fetchrow(
        sql,
        type, content,
        json.dumps(embedding) if embedding else None,
        importance, emotional_valence, trust_level,
        priority, half_life_hours,
        json.dumps(tags), json.dumps(context),
    )
    return {"id": str(row["id"]), "created_at": str(row["created_at"]), "embedded": embedding is not None}


async def recall(
    query: str,
    limit: int = 10,
    min_importance: float = 0.3,
    max_importance: float = 1.0,
    memory_type: Optional[str] = None,
) -> list[dict]:
    """Semantic recall: vector similarity + fallback to full-text search."""
    embedding = embed(query)

    if embedding:
        type_filter = "AND type = $6::memory_type" if memory_type else ""
        sql = f"""
            SELECT id, type, content, importance, emotional_valence,
                   trust_level, tags, created_at,
                   (embedding <=> $1::vector) AS distance
            FROM memories
            WHERE status = 'active'
              AND importance >= $2
              AND importance <= $3
              AND embedding IS NOT NULL
              {type_filter}
            ORDER BY embedding <=> $1::vector
            LIMIT $4
        """
        args = [json.dumps(embedding), min_importance, max_importance, limit]
        if memory_type:
            args.append(memory_type)
        rows = await db.fetch(sql, *args)
    else:
        log.warning("recall: no embedding, falling back to full-text")
        rows = await db.fetch(
            """SELECT * FROM full_text_search($1, $2)
               WHERE importance >= $3 AND importance <= $4""",
            query, limit, min_importance, max_importance,
        )

    return [_row_to_dict(r) for r in rows]


async def recall_recent(limit: int = 10) -> list[dict]:
    """Return the most recently created active memories."""
    rows = await db.fetch(
        """SELECT id, type, content, importance, emotional_valence,
                  trust_level, tags, created_at
           FROM memories WHERE status = 'active'
           ORDER BY created_at DESC LIMIT $1""",
        limit,
    )
    return [_row_to_dict(r) for r in rows]


async def hydrate(query: str, limit: int = 10) -> dict:
    """Full context reconstruction: identity + worldview + diary + memories."""
    embedding = embed(query)
    embed_literal = json.dumps(embedding) if embedding else None

    result = await db.fetchval(
        "SELECT hydrate_context($1::vector, $2)",
        embed_literal, limit,
    )
    return json.loads(result) if result else {}


async def remember_batch(items: list[dict]) -> list[dict]:
    """Bulk insert a list of memory dicts."""
    results = []
    for item in items:
        result = await remember(**item)
        results.append(result)
    return results


async def edit(
    id: str,
    content: Optional[str] = None,
    importance: Optional[float] = None,
    emotional_valence: Optional[float] = None,
    trust_level: Optional[float] = None,
    half_life_hours: Optional[int] = None,
    tags: Optional[list] = None,
    status: Optional[str] = None,
) -> dict:
    """
    Partial update a memory. Only supplied fields are changed.
    created_at is never touched.
    If content changes, the embedding is regenerated via Ollama.
    If content is unchanged, the existing embedding is preserved.
    """
    # Verify memory exists
    existing = await db.fetchrow(
        "SELECT id, content, embedding FROM memories WHERE id = $1::uuid",
        id,
    )
    if not existing:
        return {"error": f"Memory {id} not found"}

    # Build SET clauses dynamically — only what was passed
    clauses = ["updated_at = NOW()"]
    values: list = []
    idx = 1

    def add(col: str, val, cast: str = ""):
        nonlocal idx
        clauses.append(f"{col} = ${idx}{cast}")
        values.append(val)
        idx += 1

    re_embedded = False
    if content is not None and content != existing["content"]:
        embedding = embed(content)
        add("content", content)
        add("embedding", json.dumps(embedding) if embedding else None, "::vector")
        re_embedded = True
    elif content is not None:
        # content passed but unchanged — no re-embed needed
        add("content", content)

    if importance is not None:
        importance = max(0.0, min(1.0, importance))
        add("importance", importance)
    if emotional_valence is not None:
        emotional_valence = max(-1.0, min(1.0, emotional_valence))
        add("emotional_valence", emotional_valence)
    if trust_level is not None:
        trust_level = max(0.0, min(1.0, trust_level))
        add("trust_level", trust_level)
    if half_life_hours is not None:
        add("half_life_hours", half_life_hours)
    if tags is not None:
        add("tags", json.dumps(tags), "::jsonb")
    if status is not None:
        add("status", status, "::memory_status")

    if len(clauses) == 1:
        return {"id": id, "updated": False, "message": "No fields to update"}

    sql = f"""
        UPDATE memories SET {', '.join(clauses)}
        WHERE id = ${idx}::uuid
        RETURNING id, content, importance, emotional_valence,
                  trust_level, tags, status, updated_at
    """
    values.append(id)

    row = await db.fetchrow(sql, *values)
    result = _row_to_dict(row)
    result["re_embedded"] = re_embedded
    return result


async def delete(id: str, hard: bool = False) -> dict:
    """
    Delete a memory.
    Default (hard=False): soft delete — sets status='deleted', preserves the row.
    hard=True: permanent removal from the database. Use with consent_check first.
    """
    if hard:
        status = await db.execute(
            "DELETE FROM memories WHERE id = $1::uuid",
            id,
        )
        deleted = status.endswith("1")
        return {"id": id, "deleted": deleted, "hard": True}
    else:
        row = await db.fetchrow(
            """UPDATE memories SET status = 'deleted', updated_at = NOW()
               WHERE id = $1::uuid AND status != 'deleted'
               RETURNING id, status""",
            id,
        )
        if row:
            return {"id": id, "deleted": True, "hard": False, "status": "deleted"}
        return {"id": id, "deleted": False, "message": "Not found or already deleted"}


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            d[k] = str(v)
    return d
