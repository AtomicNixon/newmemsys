"""
init_db.py — Apply schema files to the NewMemSys Docker database.

Usage:
    python scripts/init_db.py              # Phase 1 schema only
    python scripts/init_db.py --with-age   # Phase 1 + AGE graph layer

Reads connection info from .env in the project root (optional).
Falls back to environment variables or hardcoded defaults.

Safe to re-run: all SQL files are idempotent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Load .env if present
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

PHASE1_FILES = [
    ROOT / "db" / "01_schema.sql",
    ROOT / "db" / "02_functions.sql",
    ROOT / "db" / "03_views.sql",
    ROOT / "db" / "04_heartbeat.sql",
]

AGE_FILE = ROOT / "db" / "05_age_graph.sql"


def apply_sql(conn, path: Path) -> None:
    print(f"  Applying {path.name} ...", end=" ", flush=True)
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("OK")


def check_age_available(conn) -> bool:
    """Check whether AGE is installed in the current PostgreSQL instance."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_available_extensions WHERE name = 'age'"
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def main() -> None:
    with_age = "--with-age" in sys.argv

    print(f"\nConnecting to {DB_CONFIG['host']}:{DB_CONFIG['port']}"
          f"/{DB_CONFIG['dbname']} ...")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
    except psycopg2.OperationalError as e:
        print(f"\nERROR: Cannot connect to database.\n{e}")
        print("\nMake sure Docker is running:  docker compose up -d")
        sys.exit(1)

    print("Connected.\n")

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    print("Phase 1 — core schema:")
    for sql_file in PHASE1_FILES:
        try:
            apply_sql(conn, sql_file)
        except Exception as e:
            conn.rollback()
            print(f"FAILED\n{e}")
            sys.exit(1)

    # ── Phase 2 (AGE) ─────────────────────────────────────────────────────────
    if with_age:
        print("\nPhase 2 — Apache AGE graph layer:")

        if not check_age_available(conn):
            print(
                "\n  ERROR: AGE extension not available in this PostgreSQL instance.\n"
                "  You need to rebuild the Docker image first:\n\n"
                "      docker compose down\n"
                "      docker compose build\n"
                "      docker compose up -d\n"
                "  Then re-run:  python scripts/init_db.py --with-age\n"
            )
            conn.close()
            sys.exit(1)

        try:
            apply_sql(conn, AGE_FILE)
        except Exception as e:
            conn.rollback()
            print(f"FAILED\n{e}")
            sys.exit(1)

        print("\n  AGE graph layer installed.")
        print("  To migrate existing memories and edges into the graph:")
        print("    SELECT * FROM sync_memories_to_age();")
        print("    SELECT * FROM sync_edges_to_age();")
        print("    SELECT * FROM age_graph_stats;")
    else:
        print("\n  (Skipping AGE graph layer. Re-run with --with-age after")
        print("   rebuilding the Docker image to enable Phase 2.)")

    conn.close()
    print("\nDatabase initialized successfully.")
    print("Run  python scripts/verify.py  to confirm everything works.\n")


if __name__ == "__main__":
    main()
