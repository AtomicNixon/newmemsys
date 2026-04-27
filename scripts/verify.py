"""
verify.py — End-to-end sanity check for the NewMemSys installation.

Usage:
    python scripts/verify.py

Checks:
  1. PostgreSQL connection (port 5433)
  2. All expected tables exist
  3. pgvector extension loaded
  4. Identity table seeded
  5. Ollama reachable + 768-dim embedding returned
  6. remember → recall round-trip
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

env_path = ROOT / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

import psycopg2

DB_CONFIG = dict(
    host=os.getenv("POSTGRES_HOST", "localhost"),
    port=int(os.getenv("POSTGRES_PORT", "5433")),
    dbname=os.getenv("POSTGRES_DB", "memory_system"),
    user=os.getenv("POSTGRES_USER", "memory_user"),
    password=os.getenv("POSTGRES_PASSWORD", "memsys_secure_2026"),
)
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

EXPECTED_TABLES = [
    "memories", "clusters", "memory_cluster_map", "memory_graph",
    "diary", "identity", "worldview", "goals", "drives",
    "outbox", "heartbeat_config",
]

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok, detail))
    status = PASS if ok else FAIL
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))


def main() -> None:
    print("\n=== NewMemSys Verification ===\n")

    # 1. DB connection
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("SELECT version()")
        version = cur.fetchone()[0].split(",")[0]
        check("PostgreSQL connection", True, version)
    except Exception as e:
        check("PostgreSQL connection", False, str(e))
        print("\nCannot continue without DB. Is Docker running?  docker compose up -d")
        sys.exit(1)

    # 2. Tables
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
    """)
    existing = {r[0] for r in cur.fetchall()}
    for t in EXPECTED_TABLES:
        check(f"Table: {t}", t in existing)

    # 3. pgvector
    cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
    check("pgvector extension", cur.fetchone() is not None)

    # 4. Identity seeded
    cur.execute("SELECT count(*) FROM identity")
    count = cur.fetchone()[0]
    check("Identity seeded", count >= 4, f"{count} rows")

    # 5. Ollama
    import urllib.request, urllib.error
    try:
        payload = json.dumps({"model": OLLAMA_MODEL, "input": "verify"}).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            embeddings = data.get("embeddings")
            vec = embeddings[0] if (embeddings and isinstance(embeddings, list)) else data.get("embedding", [])
            check("Ollama embedding (768-dim)", len(vec) == 768, f"got {len(vec)} dims")
    except Exception as e:
        check("Ollama embedding", False, str(e))
        vec = None

    # 6. remember → recall round-trip
    test_id = None
    try:
        embed_val = json.dumps(vec) if vec else None
        cur.execute(
            """INSERT INTO memories (type, content, embedding, importance, tags)
               VALUES ('semantic', 'verify_test_memory_do_not_keep', %s::vector, 0.1, %s::jsonb)
               RETURNING id""",
            (embed_val, json.dumps(["_verify"])),
        )
        test_id = cur.fetchone()[0]
        conn.commit()
        check("remember (INSERT)", True, str(test_id))

        cur.execute(
            "SELECT id, content FROM memories WHERE id = %s",
            (test_id,),
        )
        row = cur.fetchone()
        check("recall (SELECT)", row is not None and "verify_test" in row[1])
    except Exception as e:
        check("remember/recall round-trip", False, str(e))
    finally:
        if test_id:
            try:
                cur.execute("DELETE FROM memories WHERE id = %s", (test_id,))
                conn.commit()
            except Exception:
                pass

    conn.close()

    # Summary
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{'='*34}")
    print(f"  {passed}/{total} checks passed")
    if passed == total:
        print("  All checks passed. System is ready.")
        print("\n  Next step: copy claude_mcp_config.json into Claude Desktop settings.")
    else:
        print("  Some checks failed — review output above.")
    print()


if __name__ == "__main__":
    main()
