-- =============================================================================
-- db/05_age_graph.sql — Apache AGE Graph Layer  (Phase 2)
-- =============================================================================
-- Prerequisites:
--   • Dockerfile with AGE compiled and shared_preload_libraries='age'
--   • PostgreSQL restarted after image rebuild
--
-- Run via:
--   python scripts/init_db.py --with-age
-- Or manually:
--   psql -U memory_user -d memory_system -f db/05_age_graph.sql
--
-- Safe to re-run: all statements are idempotent.
-- =============================================================================

-- Load and install AGE
LOAD 'age';
CREATE EXTENSION IF NOT EXISTS age;

-- AGE requires ag_catalog in the search path for every session that uses it.
-- We set it here for this script; application code must also set it per
-- connection (handled in graph_age.py).
SET search_path = ag_catalog, "$user", public;

-- =============================================================================
-- GRAPH: cognitive_graph
-- =============================================================================
-- One graph for the entire memory system. Vertices = memories, worldview
-- entries, goals, drives. Edges = typed relationships from memory_graph +
-- worldview contradictions + goal dependencies (Phase 2+).

SELECT CASE
    WHEN NOT EXISTS (
        SELECT 1 FROM ag_graph WHERE name = 'cognitive_graph'
    )
    THEN ag_catalog.create_graph('cognitive_graph')
END;

-- =============================================================================
-- VERTEX LABELS
-- Each label maps to a PostgreSQL table of the same logical type.
-- =============================================================================

DO $age_labels$
DECLARE
    labels TEXT[] := ARRAY[
        'Memory',       -- mirrors memories table
        'WorldView',    -- mirrors worldview table
        'Goal',         -- mirrors goals table
        'Drive'         -- mirrors drives table
    ];
    lbl TEXT;
BEGIN
    FOREACH lbl IN ARRAY labels LOOP
        IF NOT EXISTS (
            SELECT 1 FROM ag_label
            WHERE graph = (SELECT graphid FROM ag_graph WHERE name = 'cognitive_graph')
              AND name = lbl
        ) THEN
            PERFORM ag_catalog.create_vlabel('cognitive_graph', lbl);
            RAISE NOTICE 'Created vertex label: %', lbl;
        END IF;
    END LOOP;
END;
$age_labels$;

-- =============================================================================
-- EDGE LABELS
-- One label per relationship_type enum value, plus higher-order types.
-- =============================================================================

DO $age_edges$
DECLARE
    labels TEXT[] := ARRAY[
        'CAUSES',
        'CAUSED_BY',
        'RELATED_TO',
        'CONTRADICTS',
        'SUPPORTS',
        'PRECEDES',
        'FOLLOWS',
        'PART_OF',
        'EXAMPLE_OF',
        'INFORMS_BELIEF',   -- memory → worldview
        'DRIVES_GOAL'       -- drive → goal
    ];
    lbl TEXT;
BEGIN
    FOREACH lbl IN ARRAY labels LOOP
        IF NOT EXISTS (
            SELECT 1 FROM ag_label
            WHERE graph = (SELECT graphid FROM ag_graph WHERE name = 'cognitive_graph')
              AND name = lbl
        ) THEN
            PERFORM ag_catalog.create_elabel('cognitive_graph', lbl);
            RAISE NOTICE 'Created edge label: %', lbl;
        END IF;
    END LOOP;
END;
$age_edges$;

-- =============================================================================
-- MIGRATION FUNCTION: sync_memories_to_age()
-- Copies all active memories from the memories table into AGE vertices.
-- Idempotent: skips memories already present (matched by pg_id property).
-- =============================================================================

CREATE OR REPLACE FUNCTION sync_memories_to_age()
RETURNS TABLE(inserted INT, skipped INT) AS $func$
DECLARE
    mem         RECORD;
    v_count     INT := 0;
    s_count     INT := 0;
    exists_val  BOOL;
    cypher_q    TEXT;
BEGIN
    FOR mem IN
        SELECT id, type, content, importance, emotional_valence,
               trust_level, status, created_at
        FROM memories
        WHERE status = 'active'
    LOOP
        -- Check if vertex already exists for this pg id
        SELECT EXISTS (
            SELECT 1 FROM cypher('cognitive_graph', $$
                MATCH (m:Memory {pg_id: %L})
                RETURN m
            $$) AS (m agtype)
        ) INTO exists_val
        USING mem.id::text;

        IF NOT exists_val THEN
            cypher_q := format(
                $cypher$
                SELECT * FROM cypher('cognitive_graph', $$
                    CREATE (m:Memory {
                        pg_id: '%s',
                        type: '%s',
                        content: %L,
                        importance: %s,
                        emotional_valence: %s,
                        trust_level: %s,
                        created_at: '%s'
                    })
                $$) AS (m agtype)
                $cypher$,
                mem.id,
                mem.type,
                mem.content,
                mem.importance,
                mem.emotional_valence,
                mem.trust_level,
                mem.created_at
            );
            EXECUTE cypher_q;
            v_count := v_count + 1;
        ELSE
            s_count := s_count + 1;
        END IF;
    END LOOP;

    RETURN QUERY SELECT v_count, s_count;
END;
$func$ LANGUAGE plpgsql;

-- =============================================================================
-- MIGRATION FUNCTION: sync_edges_to_age()
-- Copies all edges from memory_graph into AGE typed edges.
-- Requires vertices to exist first — run sync_memories_to_age() before this.
-- =============================================================================

CREATE OR REPLACE FUNCTION sync_edges_to_age()
RETURNS TABLE(inserted INT, skipped INT) AS $func$
DECLARE
    edge        RECORD;
    e_count     INT := 0;
    s_count     INT := 0;
    exists_val  BOOL;
    edge_label  TEXT;
BEGIN
    FOR edge IN
        SELECT g.id, g.memory_id, g.connected_memory_id,
               g.relationship_type, g.confidence, g.context
        FROM memory_graph g
    LOOP
        -- Map relationship_type enum → AGE edge label (uppercase)
        edge_label := upper(edge.relationship_type::text);

        -- Check if this edge already exists
        SELECT EXISTS (
            SELECT 1 FROM cypher('cognitive_graph', $$
                MATCH (a:Memory {pg_id: %L})-[r]->(b:Memory {pg_id: %L})
                WHERE r.pg_id = %L
                RETURN r
            $$) AS (r agtype)
        ) INTO exists_val
        USING edge.memory_id::text, edge.connected_memory_id::text, edge.id::text;

        IF NOT exists_val THEN
            EXECUTE format(
                $cypher$
                SELECT * FROM cypher('cognitive_graph', $$
                    MATCH (a:Memory {pg_id: '%s'}), (b:Memory {pg_id: '%s'})
                    CREATE (a)-[r:%s {
                        pg_id: '%s',
                        confidence: %s,
                        context: %L
                    }]->(b)
                $$) AS (r agtype)
                $cypher$,
                edge.memory_id,
                edge.connected_memory_id,
                edge_label,
                edge.id,
                edge.confidence,
                COALESCE(edge.context, '')
            );
            e_count := e_count + 1;
        ELSE
            s_count := s_count + 1;
        END IF;
    END LOOP;

    RETURN QUERY SELECT e_count, s_count;
END;
$func$ LANGUAGE plpgsql;

-- =============================================================================
-- CONVENIENCE VIEW: age_graph_stats
-- =============================================================================

CREATE OR REPLACE VIEW age_graph_stats AS
SELECT
    (SELECT count(*) FROM cypher('cognitive_graph',
        $$ MATCH (n:Memory) RETURN n $$) AS (n agtype)) AS memory_vertices,
    (SELECT count(*) FROM cypher('cognitive_graph',
        $$ MATCH ()-[r]->() RETURN r $$) AS (r agtype)) AS total_edges,
    (SELECT count(*) FROM memory_graph) AS pg_edges,
    (SELECT count(*) FROM memories WHERE status = 'active') AS pg_memories;

-- =============================================================================
-- NOTE: To migrate existing data after first AGE setup, run:
--
--   SELECT * FROM sync_memories_to_age();
--   SELECT * FROM sync_edges_to_age();
--   SELECT * FROM age_graph_stats;
--
-- Expected output for a populated system:
--   memory_vertices | total_edges | pg_edges | pg_memories
--   ----------------+-------------+----------+-------------
--   572             | 9321        | 9321     | 572
-- =============================================================================
