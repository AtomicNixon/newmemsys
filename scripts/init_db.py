"""
init_db.py — Apply schema files to the NewMemSys Docker database.

Usage:
    python scripts/init_db.py

Reads connection info from .env in the project root.
Applies db/01_schema.sql, db/02_functions.sql, db/03_views.sql in order.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running from repo root or scripts/
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

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

DB_CONFIG = dict(
    host=os.getenv("POSTGRES_HOST", "localhost"),
    port=int(os.getenv("POSTGRES_PORT", "5433")),
    dbname=os.getenv("POSTGRES_DB", "memory_system"),
    user=os.getenv("POSTGRES_USER", "memory_user"),
    password=os.getenv("POSTGRES_PASSWORD", "memsys_secure_2026"),
)

SQL_FILES = [
    ROOT / "db" / "01_schema.sql",
    ROOT / "db" / "02_functions.sql",
    ROOT / "db" / "03_views.sql",
    ROOT / "db" / "04_heartbeat.sql",
]


def apply_sql(conn, path: Path) -> None:
    print(f"  Applying {path.name} ...", end=" ")
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("OK")


def main() -> None:
    print(f"\nConnecting to {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']} ...")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
    except psycopg2.OperationalError as e:
        print(f"\nERROR: Cannot connect to database.\n{e}")
        print("\nMake sure Docker is running:  docker compose up -d")
        sys.exit(1)

    print("Connected.\n")

    for sql_file in SQL_FILES:
        try:
            apply_sql(conn, sql_file)
        except Exception as e:
            conn.rollback()
            print(f"FAILED\n{e}")
            sys.exit(1)

    conn.close()
    print("\nDatabase initialized successfully.")
    print("Run  python scripts/verify.py  to confirm everything works.\n")


if __name__ == "__main__":
    main()
