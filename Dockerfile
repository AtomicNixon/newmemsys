# =============================================================================
# NewMemSys brain image — Phase 2
# Base: pgvector/pgvector:pg16 (ships with pgvector pre-installed)
# Added: Apache AGE v1.5.0-rc0 (Cypher graph queries on PG16)
#
# Build:  docker compose build
# Run:    docker compose up -d
#
# NOTE: The data volume (newmemsys_pgdata) is preserved across image rebuilds.
#       Existing Phase 1 data is safe. After rebuilding, run:
#           python scripts/init_db.py --with-age
#       to install the AGE extension and create the cognitive graph.
# =============================================================================

FROM pgvector/pgvector:pg16

# ── Build dependencies ────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        postgresql-server-dev-16 \
        bison \
        flex \
    && rm -rf /var/lib/apt/lists/*

# ── Apache AGE v1.5.0-rc0 (PG16 branch) ──────────────────────────────────────
RUN git clone --depth 1 \
        --branch PG16/v1.5.0-rc0 \
        https://github.com/apache/age.git /tmp/age \
    && cd /tmp/age \
    && make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config \
    && make install PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config \
    && rm -rf /tmp/age

# ── Tell PostgreSQL to preload AGE at startup ─────────────────────────────────
# Appending to the sample config means any container started from this image
# will have AGE available without a manual postgresql.conf edit.
RUN echo "shared_preload_libraries = 'age'" \
        >> /usr/share/postgresql/postgresql.conf.sample

# ── Remove build tools from final image ──────────────────────────────────────
RUN apt-get purge -y --auto-remove \
        build-essential \
        git \
        postgresql-server-dev-16 \
        bison \
        flex \
    && rm -rf /var/lib/apt/lists/*
