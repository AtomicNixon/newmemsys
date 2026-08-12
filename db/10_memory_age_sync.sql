-- =============================================================================
-- db/10_memory_age_sync.sql — Auto-sync memories INSERT/UPDATE to AGE
-- =============================================================================
-- Root cause fix: sync_memories_to_age() already existed (05_age_graph.sql)
-- but had only ever been run once against a partial backlog (607/896
-- active memories had vertices, 289 did not) -- same "never ran against
-- the full backlog" pattern as the worldview gap fixed in 43332dd.
--
-- This migration adds a per-row trigger (mirrors tg_sync_worldview_to_age
-- from 09_worldview_age_sync.sql) so new/updated memories stay synced
-- going forward. Attached to memories_base (the real backing table of the
-- computed-decay `memories` view, see 08_computed_decay.sql) since regular
-- AFTER triggers cannot be created directly on a view -- writes through
-- the view are routed to memories_base by its INSTEAD OF triggers, so an
-- AFTER trigger on memories_base fires for every real write regardless of
-- whether it came through the view.
--
-- Only active memories get vertices, matching sync_memories_to_age()'s
-- WHERE status = 'active' filter.
--
-- Safe to re-run: idempotent (CREATE OR REPLACE, DROP TRIGGER IF EXISTS).
-- =============================================================================

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

CREATE OR REPLACE FUNCTION trg_sync_memory_to_age()
RETURNS TRIGGER AS $func$
DECLARE
    exists_val  BOOL;
    content_esc TEXT;
BEGIN
    EXECUTE 'LOAD ''age''';

    IF NEW.status <> 'active' THEN
        RETURN NEW;
    END IF;

    content_esc := replace(replace(COALESCE(NEW.content, ''), '\', '\\'), '''', '\''');

    EXECUTE format(
        $q$SELECT EXISTS (
            SELECT 1 FROM cypher('cognitive_graph', $$
                MATCH (m:Memory {pg_id: %L})
                RETURN m
            $$) AS (m agtype))$q$,
        NEW.id::text
    ) INTO exists_val;

    -- Uses importance_original (not the decayed `importance` view column,
    -- which isn't available on this base table) -- at write time elapsed
    -- decay is zero anyway, so this matches what sync_memories_to_age()
    -- would compute for a just-written row.
    IF NOT exists_val THEN
        EXECUTE format(
            $cypher$SELECT * FROM cypher('cognitive_graph', $$
                CREATE (m:Memory {
                    pg_id: %L,
                    type: %L,
                    content: '%s',
                    importance: %s,
                    emotional_valence: %s,
                    trust_level: %s,
                    created_at: %L
                })
            $$) AS (m agtype)$cypher$,
            NEW.id::text,
            NEW.type::text,
            content_esc,
            NEW.importance_original,
            NEW.emotional_valence,
            NEW.trust_level,
            NEW.created_at::text
        );
    ELSE
        EXECUTE format(
            $cypher$SELECT * FROM cypher('cognitive_graph', $$
                MATCH (m:Memory {pg_id: '%s'})
                SET m.type = '%s',
                    m.content = '%s',
                    m.importance = %s,
                    m.emotional_valence = %s,
                    m.trust_level = %s
                RETURN m
            $$) AS (m agtype)$cypher$,
            NEW.id::text,
            NEW.type::text,
            content_esc,
            NEW.importance_original,
            NEW.emotional_valence,
            NEW.trust_level
        );
    END IF;

    RETURN NEW;
END;
$func$ LANGUAGE plpgsql SET search_path = ag_catalog, public;

DROP TRIGGER IF EXISTS tg_sync_memory_to_age ON memories_base;
CREATE TRIGGER tg_sync_memory_to_age
    AFTER INSERT OR UPDATE ON memories_base
    FOR EACH ROW
    EXECUTE FUNCTION trg_sync_memory_to_age();
