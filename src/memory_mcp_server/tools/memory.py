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


RECALL_FIELDS_ALL = (
    "id, type, content, importance, emotional_valence, "
    "trust_level, tags, created_at"
)
RECALL_FIELDS_SLIM = "id, content, importance, emotional_valence"

VALID_FIELDS = {"id", "type", "content", "importance", "emotional_valence",
                "trust_level", "tags", "created_at"}


async def recall(
    query: str,
    limit: int = 10,
    min_importance: float = 0.3,
    max_importance: float = 1.0,
    memory_type: Optional[str] = None,
    fields: Optional[list[str]] = None,
) -> list[dict]:
    """Semantic recall: vector similarity + fallback to full-text search.

    fields: optional list of columns to return. Omit for all columns.
            Valid: id, type, content, importance, emotional_valence,
                   trust_level, tags, created_at.
            Tip: fields=["id","content","importance","emotional_valence"]
                 halves payload size — useful for bulk valence sweeps.
    """
    embedding = embed(query)

    # Build SELECT clause
    if fields:
        safe = [f for f in fields if f in VALID_FIELDS]
        if "id" not in safe:
            safe.insert(0, "id")          # always include id
        select = ", ".join(safe)
        distance_col = ""
    else:
        select = RECALL_FIELDS_ALL
        distance_col = ", (embedding <=> $1::vector) AS distance"

    if embedding:
        type_filter = "AND type = $6::memory_type" if memory_type else ""
        sql = f"""
            SELECT {select}{distance_col}
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
            f"""SELECT {select} FROM full_text_search($1, $2)
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


async def hydrate_light() -> dict:
    """Lightweight session start: identity keys + last 2 diary entries only.

    Use instead of hydrate() when full context reconstruction is not needed —
    short sessions, quick lookups, or when you already know the context.
    Saves significant tokens vs hydrate().
    """
    pool = await db.get_pool()

    identity_rows = await pool.fetch(
        "SELECT key, value, priority FROM identity ORDER BY priority DESC"
    )
    identity = {r["key"]: r["value"] for r in identity_rows}

    diary_rows = await pool.fetch(
        """SELECT date, mood, entry FROM diary
           ORDER BY date DESC, created_at DESC LIMIT 2"""
    )
    diary = [dict(r) for r in diary_rows]
    for d in diary:
        if hasattr(d.get("date"), "isoformat"):
            d["date"] = d["date"].isoformat()

    return {
        "identity": identity,
        "recent_diary": diary,
        "note": "Light hydration — identity + last 2 diary entries. "
                "Call hydrate(query) for full context including memories.",
    }


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


async def edit_batch(items: list[dict]) -> dict:
    """Bulk partial-update multiple memories in one call.

    Each item must have 'id' plus any fields to change:
      importance, emotional_valence, trust_level, half_life_hours, tags, status.
    Content changes (which trigger re-embedding) are supported but slow —
    for pure valence/importance sweeps omit content entirely.

    Returns a summary: updated count, skipped count, any errors.
    """
    updated = []
    skipped = []
    errors = []

    for item in items:
        item_id = item.get("id")
        if not item_id:
            errors.append({"item": item, "error": "missing id"})
            continue
        try:
            # Pass only recognised edit fields
            kwargs = {k: v for k, v in item.items()
                      if k in ("id", "content", "importance", "emotional_valence",
                               "trust_level", "half_life_hours", "tags", "status")}
            result = await edit(**kwargs)
            if result.get("error"):
                errors.append({"id": item_id, "error": result["error"]})
            elif not result.get("updated", True):
                skipped.append(item_id)
            else:
                updated.append(item_id)
        except Exception as e:
            errors.append({"id": item_id, "error": str(e)})

    return {
        "updated": len(updated),
        "skipped": len(skipped),
        "errors":  errors,
        "updated_ids": updated,
    }


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
