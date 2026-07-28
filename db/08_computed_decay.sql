-- ============================================================================
-- 08_computed_decay.sql
--
-- Refactor: importance is now COMPUTED at read time, never written by decay.
--
-- This deletes the entire category of "decay bug" — no future decay-logic
-- error can ever be destructive because decay is no longer a mutation.
--
-- Changes:
--   1. Rename memories → memories_base (the stored table)
--   2. Add importance_original FLOAT (set once at insert, never touched by decay)
--   3. Add last_recalled_at TIMESTAMPTZ (bumped only by recall(), the real
--      reinforcement signal — separate from updated_at which is bumped by
--      edits, decay, bridge sync, etc.)
--   4. Backfill importance_original from current importance (post-reset values)
--   5. Backfill last_recalled_at from updated_at (best available for existing)
--   6. Create VIEW memories that computes importance live:
--        importance = importance_original * 0.5^(hours_since_last_touch / half_life)
--      where last_touch = COALESCE(last_recalled_at, updated_at)
--   7. INSTEAD OF INSERT trigger routes importance → importance_original
--   8. INSTEAD OF UPDATE trigger routes importance → importance_original
--   9. Drop decay_importance() — no longer needed, decay is computed
--
-- All existing queries against `memories` keep working unchanged.
-- INSERT/UPDATE statements that set `importance` route to `importance_original`.
-- SELECT statements that read `importance` get the computed live value.
--
-- The heartbeat's decay task becomes a no-op (or is removed in code).
-- ============================================================================

-- ── Step 1: Rename table ────────────────────────────────────────────────────
ALTER TABLE memories RENAME TO memories_base;

-- ── Step 2: Add importance_original ─────────────────────────────────────────
ALTER TABLE memories_base ADD COLUMN importance_original FLOAT
    CHECK (importance_original BETWEEN 0.0 AND 1.0) DEFAULT 0.5;

-- Backfill from current importance (post-reset, so this captures Bob's
-- calibrated values for scored memories and the type-based defaults for
-- the reset ones)
UPDATE memories_base SET importance_original = importance
WHERE importance_original = 0.5 AND importance != 0.5;

-- For any rows where importance was already 0.5 (legitimate default), make
-- sure importance_original is set correctly
UPDATE memories_base SET importance_original = importance
WHERE importance_original IS NULL OR importance_original = 0.5;

-- ── Step 3: Add last_recalled_at ────────────────────────────────────────────
ALTER TABLE memories_base ADD COLUMN last_recalled_at TIMESTAMPTZ;

-- Backfill from updated_at — best we can do for existing memories.
-- Going forward, only recall()/recall_recent() bump this.
UPDATE memories_base SET last_recalled_at = updated_at
WHERE last_recalled_at IS NULL;

-- ── Step 4: Create the VIEW ─────────────────────────────────────────────────
-- This is what all existing queries hit. importance is computed live.
CREATE OR REPLACE VIEW memories AS
SELECT
    id, type, content, embedding,
    -- COMPUTED importance: importance_original decayed by half-life since last touch
    -- last_touch = COALESCE(last_recalled_at, updated_at, created_at)
    importance_original * POWER(0.5,
        EXTRACT(EPOCH FROM (NOW() - COALESCE(last_recalled_at, updated_at, created_at))) / 3600.0
        / half_life_hours::FLOAT
    ) AS importance,
    emotional_valence, trust_level, priority, half_life_hours,
    status, created_at, updated_at, created_by, context, tags,
    vestige_node_id, importance_original, last_recalled_at
FROM memories_base;

-- ── Step 5: INSTEAD OF INSERT trigger ───────────────────────────────────────
-- Routes incoming importance → importance_original
CREATE OR REPLACE FUNCTION memories_insert_trg()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- If importance is not provided, use the default 0.5
    IF NEW.importance IS NULL THEN
        NEW.importance := 0.5;
    END IF;
    INSERT INTO memories_base (
        type, content, embedding, importance_original,
        emotional_valence, trust_level, priority, half_life_hours,
        status, created_by, context, tags, vestige_node_id
    ) VALUES (
        NEW.type, NEW.content, NEW.embedding, NEW.importance,
        NEW.emotional_valence, NEW.trust_level, NEW.priority, NEW.half_life_hours,
        COALESCE(NEW.status, 'active'), NEW.created_by, NEW.context, NEW.tags,
        NEW.vestige_node_id
    )
    RETURNING id INTO NEW.id;
    RETURN NEW;
END;
$$;

CREATE TRIGGER memories_insert
    INSTEAD OF INSERT ON memories
    FOR EACH ROW EXECUTE FUNCTION memories_insert_trg();

-- ── Step 6: INSTEAD OF UPDATE trigger ───────────────────────────────────────
-- Routes importance updates → importance_original
-- Also bumps updated_at automatically (the touch_updated_at trigger on
-- memories_base still fires)
CREATE OR REPLACE FUNCTION memories_update_trg()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    UPDATE memories_base SET
        type              = COALESCE(NEW.type, type),
        content           = COALESCE(NEW.content, content),
        embedding         = COALESCE(NEW.embedding, embedding),
        importance_original = COALESCE(NEW.importance, importance_original),
        emotional_valence = COALESCE(NEW.emotional_valence, emotional_valence),
        trust_level       = COALESCE(NEW.trust_level, trust_level),
        priority          = COALESCE(NEW.priority, priority),
        half_life_hours   = COALESCE(NEW.half_life_hours, half_life_hours),
        status            = COALESCE(NEW.status, status),
        updated_at        = NOW(),
        context           = COALESCE(NEW.context, context),
        tags              = COALESCE(NEW.tags, tags),
        vestige_node_id   = COALESCE(NEW.vestige_node_id, vestige_node_id),
        last_recalled_at  = COALESCE(NEW.last_recalled_at, last_recalled_at)
    WHERE id = NEW.id;
    RETURN NEW;
END;
$$;

CREATE TRIGGER memories_update
    INSTEAD OF UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION memories_update_trg();

-- ── Step 7: INSTEAD OF DELETE trigger ───────────────────────────────────────
CREATE OR REPLACE FUNCTION memories_delete_trg()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM memories_base WHERE id = OLD.id;
    RETURN OLD;
END;
$$;

CREATE TRIGGER memories_delete
    INSTEAD OF DELETE ON memories
    FOR EACH ROW EXECUTE FUNCTION memories_delete_trg();

-- ── Step 8: Drop decay_importance() — no longer needed ──────────────────────
-- Decay is now computed at read time via the VIEW. There is nothing to write.
DROP FUNCTION IF EXISTS decay_importance(UUID);

-- ── Step 9: touch_last_recalled() helper ────────────────────────────────────
-- Called by recall()/recall_recent() to record reinforcement.
CREATE OR REPLACE FUNCTION touch_last_recalled(p_memory_id UUID)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    UPDATE memories_base SET last_recalled_at = NOW()
    WHERE id = p_memory_id;
END;
$$;
