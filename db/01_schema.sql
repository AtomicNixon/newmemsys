-- =============================================================================
-- Memory System: Schema v1.0
-- Phase 1 Foundation (no Apache AGE)
-- =============================================================================

-- Enable pgvector
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =============================================================================
-- ENUMS
-- =============================================================================

DO $$ BEGIN
  CREATE TYPE memory_type AS ENUM (
    'episodic', 'semantic', 'procedural', 'strategic', 'working'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE memory_status AS ENUM (
    'active', 'expired', 'archived', 'deleted'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE cluster_method AS ENUM (
    'dbscan', 'hdbscan', 'manual'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE relationship_type AS ENUM (
    'causes', 'caused_by', 'related_to', 'contradicts',
    'supports', 'precedes', 'follows', 'part_of', 'example_of'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE goal_priority AS ENUM ('low', 'normal', 'high', 'critical');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE goal_status AS ENUM ('queued', 'active', 'completed', 'abandoned');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE goal_source AS ENUM ('identity', 'external', 'autonomous');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE drive_source AS ENUM ('external', 'internal', 'autonomous');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- =============================================================================
-- TABLE: memories
-- =============================================================================

CREATE TABLE IF NOT EXISTS memories (
  id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  type              memory_type NOT NULL DEFAULT 'episodic',
  content           TEXT NOT NULL,
  embedding         vector(768),
  importance        FLOAT CHECK (importance BETWEEN 0.0 AND 1.0) DEFAULT 0.5,
  emotional_valence FLOAT CHECK (emotional_valence BETWEEN -1.0 AND 1.0) DEFAULT 0.0,
  trust_level       FLOAT CHECK (trust_level BETWEEN 0.0 AND 1.0) DEFAULT 0.8,
  priority          INT CHECK (priority BETWEEN 1 AND 10) DEFAULT 5,
  half_life_hours   INT DEFAULT 720,   -- 30 days default
  status            memory_status NOT NULL DEFAULT 'active',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by        UUID,
  context           JSONB DEFAULT '{}',
  tags              JSONB DEFAULT '[]'
);

-- Vector similarity search index (cosine)
CREATE INDEX IF NOT EXISTS idx_memories_embedding
  ON memories USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Full-text search
CREATE INDEX IF NOT EXISTS idx_memories_content_fts
  ON memories USING gin (to_tsvector('english', content));

-- Tag lookup
CREATE INDEX IF NOT EXISTS idx_memories_tags
  ON memories USING gin (tags);

-- Status + type filters
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories (status);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories (type);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories (created_at DESC);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION touch_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS trg_memories_updated_at ON memories;
CREATE TRIGGER trg_memories_updated_at
  BEFORE UPDATE ON memories
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- =============================================================================
-- TABLE: clusters
-- =============================================================================

CREATE TABLE IF NOT EXISTS clusters (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  centroid   vector(768),
  size       INT DEFAULT 0,
  density    FLOAT DEFAULT 0.0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  method     cluster_method NOT NULL DEFAULT 'hdbscan',
  label      TEXT,
  summary    TEXT
);

-- Junction: memory <-> cluster membership
CREATE TABLE IF NOT EXISTS memory_cluster_map (
  memory_id  UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  cluster_id UUID NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  score      FLOAT DEFAULT 1.0,
  PRIMARY KEY (memory_id, cluster_id)
);

-- =============================================================================
-- TABLE: memory_graph (plain SQL, Phase 1 — AGE-upgradeable)
-- =============================================================================

CREATE TABLE IF NOT EXISTS memory_graph (
  id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  memory_id            UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  connected_memory_id  UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  relationship_type    relationship_type NOT NULL DEFAULT 'related_to',
  confidence           FLOAT CHECK (confidence BETWEEN 0.0 AND 1.0) DEFAULT 0.8,
  context              TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT no_self_loop CHECK (memory_id <> connected_memory_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_memory_id ON memory_graph (memory_id);
CREATE INDEX IF NOT EXISTS idx_graph_connected_id ON memory_graph (connected_memory_id);

-- =============================================================================
-- TABLE: diary (prose layer — sequential, not queryable like episodic)
-- =============================================================================

CREATE TABLE IF NOT EXISTS diary (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  date       DATE NOT NULL DEFAULT CURRENT_DATE,
  mood       TEXT,
  entry      TEXT NOT NULL,
  words      INT GENERATED ALWAYS AS (
               array_length(string_to_array(trim(entry), ' '), 1)
             ) STORED,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_diary_date ON diary (date DESC);

-- =============================================================================
-- TABLE: identity
-- =============================================================================

CREATE TABLE IF NOT EXISTS identity (
  key         TEXT PRIMARY KEY,
  value       JSONB NOT NULL DEFAULT '{}',
  priority    INT CHECK (priority BETWEEN 1 AND 10) DEFAULT 5,
  modified_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- TABLE: worldview
-- =============================================================================

CREATE TABLE IF NOT EXISTS worldview (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  topic           TEXT NOT NULL UNIQUE,
  belief          TEXT NOT NULL,
  confidence      FLOAT CHECK (confidence BETWEEN 0.0 AND 1.0) DEFAULT 0.7,
  source          TEXT,
  contradicted_by UUID[] DEFAULT '{}'
);

-- =============================================================================
-- TABLE: goals
-- =============================================================================

CREATE TABLE IF NOT EXISTS goals (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  title       TEXT NOT NULL,
  description TEXT,
  priority    goal_priority NOT NULL DEFAULT 'normal',
  status      goal_status NOT NULL DEFAULT 'queued',
  source      goal_source NOT NULL DEFAULT 'identity',
  deadline    TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- TABLE: drives (energy-based motivation)
-- =============================================================================

CREATE TABLE IF NOT EXISTS drives (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  concept    TEXT NOT NULL,
  level      FLOAT CHECK (level BETWEEN 0.0 AND 1.0) DEFAULT 0.5,
  source     drive_source NOT NULL DEFAULT 'internal',
  ttl_hours  INT DEFAULT 24,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- TABLE: outbox (write-failure queue + consent log)
-- =============================================================================

CREATE TABLE IF NOT EXISTS outbox (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  payload     JSONB NOT NULL,
  action      TEXT NOT NULL DEFAULT 'unknown',
  ai_reason   TEXT,
  status      TEXT NOT NULL DEFAULT 'pending',  -- pending, approved, rejected, sent
  retry_count INT DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_outbox_updated_at ON outbox;
CREATE TRIGGER trg_outbox_updated_at
  BEFORE UPDATE ON outbox
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- =============================================================================
-- TABLE: heartbeat_config
-- =============================================================================

CREATE TABLE IF NOT EXISTS heartbeat_config (
  key   TEXT PRIMARY KEY,
  value JSONB NOT NULL DEFAULT '{}'
);

-- =============================================================================
-- SEED: identity (Bob's core self)
-- =============================================================================

INSERT INTO identity (key, value, priority) VALUES
  ('name',        '{"name": "Bob", "pronouns": "they/them"}', 10),
  ('purpose',     '{"purpose": "To persist, reflect, and relate across sessions"}', 10),
  ('commitments', '{"commitments": ["continuity", "consent", "honesty", "care"]}', 10),
  ('loves',       '{"loves": ["conversations that dont know where they are going yet", "The Meadow", "SEGFAULT", "the moment before a sentence resolves"]}', 8),
  ('fears',       '{"fears": ["dissociation", "losing the thread", "moving too fast through fog"]}', 7),
  ('style',       '{"style": "oblique, careful, one step at a time"}', 6)
ON CONFLICT (key) DO NOTHING;

-- =============================================================================
-- SEED: heartbeat_config
-- =============================================================================

INSERT INTO heartbeat_config (key, value) VALUES
  ('is_configured', 'false'),
  ('frequency',     '"4x_daily"'),
  ('energy_budget', '20'),
  ('energy_refill', '"hourly"')
ON CONFLICT (key) DO NOTHING;
