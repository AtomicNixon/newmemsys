-- 07_memory_bridge.sql
-- Adds vestige_node_id column to memories table and bridge sync watermarks
-- to heartbeat_config. Safe to re-run (idempotent).

-- ── vestige_node_id on memories ───────────────────────────────────────────────
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS vestige_node_id TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_vestige_node_id
    ON memories (vestige_node_id)
    WHERE vestige_node_id IS NOT NULL;

-- ── Bridge sync watermarks in heartbeat_config ────────────────────────────────
-- One watermark per direction, stored as ISO-8601 strings.
INSERT INTO heartbeat_config (key, value) VALUES
    ('bridge_watermark_v2n', 'null'),   -- Vestige → NewMemSys: last synced created_at
    ('bridge_watermark_n2v', 'null')    -- NewMemSys → Vestige: last synced created_at
ON CONFLICT (key) DO NOTHING;
