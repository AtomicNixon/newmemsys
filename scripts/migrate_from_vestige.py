"""
migrate_from_vestige.py — Copy Vestige memory system into NewMemSys.

What this migrates:
  knowledge_nodes   (571)  →  memories
  memory_connections(9321) →  memory_graph
  intentions        (7)    →  goals

What this skips:
  consolidation_history, dream_history, retention_snapshots — operational
  logs from Vestige's internal processes, not meaningful memories.
  fsrs_cards, compressed_memories, insights — empty or Vestige-internal.
  node_embeddings — different embedding space (all-MiniLM-L6-v2 vs
  nomic-embed-text). All content is re-embedded via Ollama.

Usage:
    python scripts/migrate_from_vestige.py [--dry-run] [--skip-embed]
                                           [--batch-size N] [--limit N]

Options:
    --dry-run      Read Vestige and report counts, do not write to NewMemSys.
    --skip-embed   Insert memories without embeddings (fast, search degrades).
    --batch-size N Commit every N memories (default 50).
    --limit N      Only migrate the first N memories (for testing).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

VESTIGE_DB = Path(r"C:\Users\Acat\AppData\Roaming\vestige\core\data\vestige.db")

# Load .env
env_path = ROOT / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

import psycopg2
import psycopg2.extras

DB_CONFIG = dict(
    host=os.getenv("POSTGRES_HOST", "localhost"),
    port=int(os.getenv("POSTGRES_PORT", "5433")),
    dbname=os.getenv("POSTGRES_DB", "memory_system"),
    user=os.getenv("POSTGRES_USER", "memory_user"),
    password=os.getenv("POSTGRES_PASSWORD", "memsys_secure_2026"),
)
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

# ---------------------------------------------------------------------------
# Colour output
# ---------------------------------------------------------------------------

BOLD   = "\033[1m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"

def info(msg):  print(f"  {CYAN}{msg}{RESET}")
def ok(msg):    print(f"  {GREEN}✓ {msg}{RESET}")
def warn(msg):  print(f"  {YELLOW}⚠ {msg}{RESET}")
def err(msg):   print(f"  {RED}✗ {msg}{RESET}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Field mappings
# ---------------------------------------------------------------------------

# Vestige node_type → NewMemSys memory_type enum
NODE_TYPE_MAP = {
    "semantic":   "semantic",
    "episodic":   "episodic",
    "procedural": "procedural",
    "strategic":  "strategic",
    "working":    "working",
    "knowledge":  "semantic",
    "fact":       "semantic",
    "skill":      "procedural",
    "event":      "episodic",
    "summary":    "semantic",
    "concept":    "semantic",
}
DEFAULT_TYPE = "semantic"

# Vestige learning_state → NewMemSys memory_status enum
LEARNING_STATE_MAP = {
    "active":      "active",
    "dormant":     "active",     # dormant = low retrieval, but still valid
    "silent":      "archived",
    "unavailable": "archived",
    "new":         "active",
    "learning":    "active",
    "review":      "active",
    "relearning":  "active",
}
DEFAULT_STATUS = "active"

# Vestige link_type → NewMemSys relationship_type enum
LINK_TYPE_MAP = {
    "associative":  "related_to",
    "causal":       "causes",
    "cause":        "causes",
    "caused_by":    "caused_by",
    "contradicts":  "contradicts",
    "contradictory":"contradicts",
    "supports":     "supports",
    "support":      "supports",
    "temporal":     "precedes",
    "precedes":     "precedes",
    "follows":      "follows",
    "part_of":      "part_of",
    "example_of":   "example_of",
    "related":      "related_to",
    "related_to":   "related_to",
    "semantic":     "related_to",
    "dream":        "related_to",
    "consolidation":"related_to",
}
DEFAULT_RELATIONSHIP = "related_to"

VALID_RELATIONSHIP_TYPES = {
    "causes", "caused_by", "related_to", "contradicts",
    "supports", "precedes", "follows", "part_of", "example_of",
}


# ---------------------------------------------------------------------------
# Embedding via Ollama
# ---------------------------------------------------------------------------

_embed_cache: dict[str, list[float]] = {}

def embed(text: str) -> list[float] | None:
    key = text[:200]
    if key in _embed_cache:
        return _embed_cache[key]

    payload = json.dumps({"model": OLLAMA_MODEL, "input": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            embeddings = data.get("embeddings")
            vec = embeddings[0] if (embeddings and isinstance(embeddings, list)) else []
            if len(vec) == 768:
                _embed_cache[key] = vec
                return vec
            warn(f"Unexpected embedding dim: {len(vec)}")
            return None
    except urllib.error.URLError as e:
        warn(f"Ollama unreachable: {e}")
        return None
    except Exception as e:
        warn(f"Embed error: {e}")
        return None


# ---------------------------------------------------------------------------
# Vestige readers
# ---------------------------------------------------------------------------

def read_nodes(src: sqlite3.Connection) -> list[dict]:
    cur = src.cursor()
    cur.execute("""
        SELECT id, content, node_type, created_at, updated_at, last_accessed,
               retention_strength, emotional_valence, sentiment_score,
               learning_state, tags, source, waking_tag, times_retrieved,
               times_useful, activation, temporal_level, summary_parent_id,
               scope, memory_system, valid_from, valid_until
        FROM knowledge_nodes
        ORDER BY created_at
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def read_connections(src: sqlite3.Connection) -> list[dict]:
    cur = src.cursor()
    cur.execute("""
        SELECT source_id, target_id, strength, link_type, created_at,
               activation_count
        FROM memory_connections
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def read_intentions(src: sqlite3.Connection) -> list[dict]:
    cur = src.cursor()
    cur.execute("""
        SELECT id, content, priority, status, created_at, deadline, tags
        FROM intentions
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Field transformers
# ---------------------------------------------------------------------------

def map_type(node_type: str | None) -> str:
    if not node_type:
        return DEFAULT_TYPE
    return NODE_TYPE_MAP.get(node_type.lower().strip(), DEFAULT_TYPE)


def map_status(state: str | None) -> str:
    if not state:
        return DEFAULT_STATUS
    return LEARNING_STATE_MAP.get(state.lower().strip(), DEFAULT_STATUS)


def map_relationship(link_type: str | None) -> str:
    if not link_type:
        return DEFAULT_RELATIONSHIP
    mapped = LINK_TYPE_MAP.get(link_type.lower().strip(), DEFAULT_RELATIONSHIP)
    return mapped if mapped in VALID_RELATIONSHIP_TYPES else DEFAULT_RELATIONSHIP


def parse_tags(raw: str | None) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(t) for t in parsed]
        return [str(parsed)]
    except Exception:
        return [t.strip() for t in raw.split(",") if t.strip()]


def clamp(val, lo=0.0, hi=1.0) -> float:
    if val is None:
        return (lo + hi) / 2
    return max(lo, min(hi, float(val)))


def parse_ts(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        # Normalize timezone-aware ISO strings
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.isoformat()
    except Exception:
        return None


def build_context(node: dict) -> dict:
    ctx = {}
    for k in ("source", "temporal_level", "scope", "memory_system",
              "times_retrieved", "times_useful", "activation",
              "summary_parent_id"):
        if node.get(k) is not None:
            ctx[k] = node[k]
    ctx["migrated_from"] = "vestige"
    ctx["vestige_id"] = node["id"]
    return ctx


def importance_from_node(node: dict) -> float:
    """
    Derive importance from Vestige's retention_strength (0-1).
    Boost for waking_tag (flashbulb) memories.
    """
    base = clamp(node.get("retention_strength") or 0.5)
    if node.get("waking_tag"):
        base = min(1.0, base + 0.2)
    return round(base, 4)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def insert_memories(
    dst: psycopg2.extensions.connection,
    nodes: list[dict],
    skip_embed: bool,
    batch_size: int,
    limit: int | None,
) -> tuple[int, int, set[str]]:
    """Returns (inserted, skipped, migrated_ids)."""
    if limit:
        nodes = nodes[:limit]

    inserted = 0
    skipped = 0
    migrated_ids: set[str] = set()

    total = len(nodes)
    cur = dst.cursor()
    t0 = time.time()

    for i, node in enumerate(nodes, 1):
        content = (node.get("content") or "").strip()
        if not content:
            skipped += 1
            continue

        mem_type   = map_type(node.get("node_type"))
        status     = map_status(node.get("learning_state"))
        importance = importance_from_node(node)
        valence    = clamp(node.get("emotional_valence") or 0.0, -1.0, 1.0)
        tags       = parse_tags(node.get("tags"))
        context    = build_context(node)
        created_at = parse_ts(node.get("created_at"))
        node_id    = node["id"]

        # Re-embed using nomic-embed-text
        embedding = None
        if not skip_embed:
            embedding = embed(content)
            if embedding is None:
                warn(f"[{i}/{total}] No embedding for node {node_id[:8]}… (will store without)")

        embed_literal = json.dumps(embedding) if embedding else None

        try:
            if created_at:
                cur.execute(
                    """
                    INSERT INTO memories
                      (id, type, content, embedding, importance, emotional_valence,
                       trust_level, priority, half_life_hours, status,
                       created_at, updated_at, tags, context)
                    VALUES
                      (%s::uuid, %s::memory_type, %s, %s::vector, %s, %s,
                       0.8, 5, 720, %s::memory_status,
                       %s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (node_id, mem_type, content, embed_literal,
                     importance, valence, status,
                     created_at, created_at,
                     json.dumps(tags), json.dumps(context)),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO memories
                      (id, type, content, embedding, importance, emotional_valence,
                       trust_level, priority, half_life_hours, status,
                       tags, context)
                    VALUES
                      (%s::uuid, %s::memory_type, %s, %s::vector, %s, %s,
                       0.8, 5, 720, %s::memory_status,
                       %s::jsonb, %s::jsonb)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (node_id, mem_type, content, embed_literal,
                     importance, valence, status,
                     json.dumps(tags), json.dumps(context)),
                )
            migrated_ids.add(node_id)
            inserted += 1
        except Exception as e:
            dst.rollback()
            warn(f"[{i}/{total}] Insert failed for {node_id[:8]}: {e}")
            skipped += 1
            cur = dst.cursor()
            continue

        # Progress and batch commit
        elapsed = time.time() - t0
        rate = i / elapsed if elapsed > 0 else 0
        eta = (total - i) / rate if rate > 0 else 0
        print(
            f"\r  [{i:>4}/{total}]  inserted={inserted}  skipped={skipped}"
            f"  {rate:.1f}/s  ETA {eta:.0f}s   ",
            end="",
        )

        if i % batch_size == 0:
            dst.commit()

    dst.commit()
    print()  # newline after progress
    return inserted, skipped, migrated_ids


def insert_edges(
    dst: psycopg2.extensions.connection,
    connections: list[dict],
    valid_ids: set[str],
) -> tuple[int, int]:
    """Returns (inserted, skipped)."""
    cur = dst.cursor()
    inserted = 0
    skipped = 0
    total = len(connections)

    for i, conn in enumerate(connections, 1):
        src_id = conn["source_id"]
        tgt_id = conn["target_id"]

        # Only link memories that were actually migrated
        if src_id not in valid_ids or tgt_id not in valid_ids:
            skipped += 1
            continue

        rel = map_relationship(conn.get("link_type"))
        confidence = clamp(conn.get("strength") or 0.5)
        created_at = parse_ts(conn.get("created_at"))
        context = f"vestige link_type={conn.get('link_type')} activations={conn.get('activation_count', 0)}"

        try:
            if created_at:
                cur.execute(
                    """
                    INSERT INTO memory_graph
                      (memory_id, connected_memory_id, relationship_type,
                       confidence, context, created_at)
                    VALUES
                      (%s::uuid, %s::uuid, %s::relationship_type, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (src_id, tgt_id, rel, confidence, context, created_at),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO memory_graph
                      (memory_id, connected_memory_id, relationship_type,
                       confidence, context)
                    VALUES
                      (%s::uuid, %s::uuid, %s::relationship_type, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (src_id, tgt_id, rel, confidence, context),
                )
            inserted += 1
        except Exception as e:
            dst.rollback()
            warn(f"Edge {src_id[:8]}→{tgt_id[:8]} failed: {e}")
            skipped += 1
            cur = dst.cursor()
            continue

        if i % 500 == 0:
            dst.commit()
            print(f"\r  [{i:>5}/{total}] edges inserted={inserted} skipped={skipped}   ", end="")

    dst.commit()
    if total > 0:
        print()
    return inserted, skipped


def insert_goals(
    dst: psycopg2.extensions.connection,
    intentions: list[dict],
) -> int:
    """Returns count inserted."""
    cur = dst.cursor()
    inserted = 0

    status_map = {
        "active":    "active",
        "fulfilled": "completed",
        "dismissed": "abandoned",
        "snoozed":   "queued",
        "pending":   "queued",
    }
    priority_map = {
        1: "low", 2: "normal", 3: "normal",
        4: "high", 5: "high", 6: "critical", 7: "critical",
    }

    for intention in intentions:
        raw_status = (intention.get("status") or "active").lower()
        status = status_map.get(raw_status, "queued")
        raw_pri = intention.get("priority") or 2
        priority = priority_map.get(int(raw_pri), "normal")
        created_at = parse_ts(intention.get("created_at"))
        deadline = parse_ts(intention.get("deadline"))

        try:
            cur.execute(
                """
                INSERT INTO goals
                  (id, title, description, priority, status, source, created_at, deadline)
                VALUES
                  (%s::uuid, %s, %s, %s::goal_priority, %s::goal_status,
                   'external', %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    intention["id"],
                    intention["content"][:200],
                    intention["content"],
                    priority, status,
                    created_at, deadline,
                ),
            )
            inserted += 1
        except Exception as e:
            dst.rollback()
            warn(f"Intention {intention['id'][:8]} failed: {e}")
            cur = dst.cursor()

    dst.commit()
    return inserted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Vestige → NewMemSys")
    parser.add_argument("--dry-run",    action="store_true", help="Read only, do not write")
    parser.add_argument("--skip-embed", action="store_true", help="Skip Ollama embedding step")
    parser.add_argument("--batch-size", type=int, default=50,  help="Commit every N memories")
    parser.add_argument("--limit",      type=int, default=None, help="Only migrate first N memories")
    args = parser.parse_args()

    print(f"\n{BOLD}=== Vestige → NewMemSys Migration ==={RESET}\n")

    # Verify Vestige DB
    if not VESTIGE_DB.exists():
        err(f"Vestige DB not found: {VESTIGE_DB}")
        sys.exit(1)
    ok(f"Vestige DB: {VESTIGE_DB}")

    src = sqlite3.connect(str(VESTIGE_DB))
    src.row_factory = sqlite3.Row

    # Read source data
    info("Reading Vestige data...")
    nodes       = read_nodes(src)
    connections = read_connections(src)
    intentions  = read_intentions(src)
    src.close()

    limit_str = f" (limited to {args.limit})" if args.limit else ""
    print(f"  knowledge_nodes    : {len(nodes):>6}{limit_str}")
    print(f"  memory_connections : {len(connections):>6}")
    print(f"  intentions         : {len(intentions):>6}")

    if args.dry_run:
        print(f"\n{YELLOW}Dry run — no writes performed.{RESET}\n")
        return

    # Connect to NewMemSys
    info("Connecting to NewMemSys PostgreSQL...")
    try:
        dst = psycopg2.connect(**DB_CONFIG)
        dst.autocommit = False
        ok(f"Connected to {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")
    except psycopg2.OperationalError as e:
        err(f"Cannot connect: {e}")
        sys.exit(1)

    # Check Ollama
    if not args.skip_embed:
        info("Checking Ollama...")
        test_vec = embed("connection test")
        if test_vec:
            ok(f"Ollama reachable — model: {OLLAMA_MODEL}")
        else:
            warn("Ollama not reachable. Memories will be stored without embeddings.")
            warn("Semantic recall (cosine search) will not work until re-embedded.")
            resp = input("  Continue without embeddings? [y/N] ").strip().lower()
            if resp != "y":
                sys.exit(0)

    # -----------------------------------------------------------------------
    # Phase 1: memories
    # -----------------------------------------------------------------------
    print(f"\n{BOLD}Phase 1: Migrating memories...{RESET}")
    t0 = time.time()
    inserted, skipped, migrated_ids = insert_memories(
        dst, nodes,
        skip_embed=args.skip_embed,
        batch_size=args.batch_size,
        limit=args.limit,
    )
    elapsed = time.time() - t0
    ok(f"Memories: {inserted} inserted, {skipped} skipped  ({elapsed:.1f}s)")

    # -----------------------------------------------------------------------
    # Phase 2: graph edges
    # -----------------------------------------------------------------------
    print(f"\n{BOLD}Phase 2: Migrating graph edges...{RESET}")
    t0 = time.time()
    e_inserted, e_skipped = insert_edges(dst, connections, migrated_ids)
    elapsed = time.time() - t0
    ok(f"Edges: {e_inserted} inserted, {e_skipped} skipped  ({elapsed:.1f}s)")

    # -----------------------------------------------------------------------
    # Phase 3: intentions → goals
    # -----------------------------------------------------------------------
    print(f"\n{BOLD}Phase 3: Migrating intentions → goals...{RESET}")
    g_inserted = insert_goals(dst, intentions)
    ok(f"Goals: {g_inserted} inserted")

    dst.close()

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print(f"\n{BOLD}{'='*45}{RESET}")
    print(f"  Memories migrated : {inserted:>6}")
    print(f"  Graph edges       : {e_inserted:>6}")
    print(f"  Goals             : {g_inserted:>6}")
    print(f"\n  Run verify to confirm:")
    print(f"    python scripts\\verify.py")
    print()


if __name__ == "__main__":
    main()
