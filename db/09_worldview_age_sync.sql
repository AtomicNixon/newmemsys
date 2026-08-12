-- =============================================================================
-- db/09_worldview_age_sync.sql — Auto-sync worldview INSERT/UPDATE to AGE
-- =============================================================================
-- Root cause fix: sync_worldview_to_age() and connect_belief_pg() already
-- existed (05_age_graph.sql) but were never run against the live worldview
-- table, so connect_belief() had no WorldView vertices to attach edges to.
-- This migration:
--   1. Adds a per-row trigger (mirrors 06_age_triggers.sql's pattern for
--      memory_graph) so new/updated beliefs stay synced going forward
--      without a manual sync_worldview_to_age() call after every write.
--   2. Adds worldview_vertices to age_graph_stats so the gap this bug
--      report was diagnosed from (memory_vertices shown, nothing for
--      worldview) doesn't recur silently.
--
-- Safe to re-run: idempotent (CREATE OR REPLACE, DROP TRIGGER IF EXISTS).
-- =============================================================================

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- =============================================================================
-- TRIGGER FUNCTION: trg_sync_worldview_to_age()
-- Upserts a single WorldView vertex on INSERT or UPDATE of worldview.
-- =============================================================================

CREATE OR REPLACE FUNCTION trg_sync_worldview_to_age()
RETURNS TRIGGER AS $func$
DECLARE
    exists_val  BOOL;
    belief_esc  TEXT;
    source_esc  TEXT;
BEGIN
    EXECUTE 'LOAD ''age''';
    belief_esc := replace(replace(COALESCE(NEW.belief, ''), '\', '\\'), '''', '\''');
    source_esc := replace(replace(COALESCE(NEW.source, ''), '\', '\\'), '''', '\''');

    EXECUTE format(
        $q$SELECT EXISTS (
            SELECT 1 FROM cypher('cognitive_graph', $$
                MATCH (w:WorldView {pg_id: %L})
                RETURN w
            $$) AS (w agtype))$q$,
        NEW.id::text
    ) INTO exists_val;

    IF NOT exists_val THEN
        EXECUTE format(
            $cypher$SELECT * FROM cypher('cognitive_graph', $$
                CREATE (w:WorldView {
                    pg_id: '%s',
                    topic: '%s',
                    belief: '%s',
                    confidence: %s,
                    source: '%s'
                })
            $$) AS (w agtype)$cypher$,
            NEW.id::text,
            NEW.topic,
            belief_esc,
            NEW.confidence,
            source_esc
        );
    ELSE
        EXECUTE format(
            $cypher$SELECT * FROM cypher('cognitive_graph', $$
                MATCH (w:WorldView {pg_id: '%s'})
                SET w.topic = '%s',
                    w.belief = '%s',
                    w.confidence = %s,
                    w.source = '%s'
                RETURN w
            $$) AS (w agtype)$cypher$,
            NEW.id::text,
            NEW.topic,
            belief_esc,
            NEW.confidence,
            source_esc
        );
    END IF;

    RETURN NEW;
END;
$func$ LANGUAGE plpgsql SET search_path = ag_catalog, public;

-- Attach trigger
DROP TRIGGER IF EXISTS tg_sync_worldview_to_age ON worldview;
CREATE TRIGGER tg_sync_worldview_to_age
    AFTER INSERT OR UPDATE ON worldview
    FOR EACH ROW
    EXECUTE FUNCTION trg_sync_worldview_to_age();

-- =============================================================================
-- Extend age_graph_stats with worldview_vertices for visibility
-- =============================================================================

DROP VIEW IF EXISTS age_graph_stats;
CREATE VIEW age_graph_stats AS
SELECT
    (SELECT count(*) FROM cypher('cognitive_graph',
        $$ MATCH (n:Memory) RETURN n $$) AS (n agtype)) AS memory_vertices,
    (SELECT count(*) FROM cypher('cognitive_graph',
        $$ MATCH (n:WorldView) RETURN n $$) AS (n agtype)) AS worldview_vertices,
    (SELECT count(*) FROM cypher('cognitive_graph',
        $$ MATCH ()-[r]->() RETURN r $$) AS (r agtype)) AS total_edges,
    (SELECT count(*) FROM memory_graph) AS pg_edges,
    (SELECT count(*) FROM memories WHERE status = 'active') AS pg_memories,
    (SELECT count(*) FROM worldview) AS pg_worldview;
