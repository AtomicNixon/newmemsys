-- =============================================================================
-- db/06_age_triggers.sql — Auto-sync memory_graph INSERTs to AGE (Phase 2)
-- =============================================================================
-- Run via:  python scripts/init_db.py --with-age
-- Or manually after 05_age_graph.sql is applied.
--
-- This trigger fires on every INSERT into memory_graph and immediately
-- creates the corresponding edge in the AGE cognitive_graph.
-- No manual sync_edges_to_age() call needed after connect().
-- =============================================================================

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- Trigger function: sync a single edge to AGE on INSERT
CREATE OR REPLACE FUNCTION trg_sync_edge_to_age()
RETURNS TRIGGER AS $func$
DECLARE
    edge_label  TEXT;
    context_esc TEXT;
    both_exist  BOOL;
BEGIN
    edge_label  := upper(NEW.relationship_type::text);
    context_esc := replace(replace(COALESCE(NEW.context, ''), '\', '\\'), '''', '\''');

    -- Check both vertices exist in AGE before creating the edge
    EXECUTE format(
        $q$SELECT EXISTS (
            SELECT 1 FROM cypher('cognitive_graph', $$
                MATCH (a:Memory {pg_id: %L}), (b:Memory {pg_id: %L})
                RETURN a, b
            $$) AS (a agtype, b agtype))$q$,
        NEW.memory_id::text,
        NEW.connected_memory_id::text
    ) INTO both_exist;

    IF both_exist THEN
        EXECUTE format(
            $cypher$SELECT * FROM cypher('cognitive_graph', $$
                MATCH (a:Memory {pg_id: '%s'}), (b:Memory {pg_id: '%s'})
                CREATE (a)-[r:%s {
                    pg_id: '%s',
                    confidence: %s,
                    context: '%s'
                }]->(b)
            $$) AS (r agtype)$cypher$,
            NEW.memory_id::text,
            NEW.connected_memory_id::text,
            edge_label,
            NEW.id::text,
            NEW.confidence,
            context_esc
        );
    END IF;

    RETURN NEW;
END;
$func$ LANGUAGE plpgsql;

-- Attach trigger
DROP TRIGGER IF EXISTS tg_sync_edge_to_age ON memory_graph;
CREATE TRIGGER tg_sync_edge_to_age
    AFTER INSERT ON memory_graph
    FOR EACH ROW
    EXECUTE FUNCTION trg_sync_edge_to_age();
