-- =============================================================================
-- Memory System: PL/pgSQL Functions v1.0
-- =============================================================================

-- =============================================================================
-- decay_importance: apply exponential half-life decay to a memory
-- =============================================================================

CREATE OR REPLACE FUNCTION decay_importance(p_memory_id UUID)
RETURNS FLOAT LANGUAGE plpgsql AS $$
DECLARE
  v_importance     FLOAT;
  v_half_life_hrs  INT;
  v_created_at     TIMESTAMPTZ;
  v_hours_elapsed  FLOAT;
  v_decayed        FLOAT;
BEGIN
  SELECT importance, half_life_hours, created_at
  INTO v_importance, v_half_life_hrs, v_created_at
  FROM memories WHERE id = p_memory_id;

  IF NOT FOUND THEN RETURN NULL; END IF;
  IF v_half_life_hrs IS NULL OR v_half_life_hrs <= 0 THEN RETURN v_importance; END IF;

  v_hours_elapsed := EXTRACT(EPOCH FROM (NOW() - v_created_at)) / 3600.0;
  v_decayed := v_importance * POWER(0.5, v_hours_elapsed / v_half_life_hrs::FLOAT);

  UPDATE memories SET importance = v_decayed, updated_at = NOW()
  WHERE id = p_memory_id;

  RETURN v_decayed;
END;
$$;

-- =============================================================================
-- expire_working_memory: expire working memories past their TTL
-- =============================================================================

CREATE OR REPLACE FUNCTION expire_working_memory()
RETURNS INT LANGUAGE plpgsql AS $$
DECLARE
  v_count INT;
BEGIN
  UPDATE memories
  SET status = 'expired', updated_at = NOW()
  WHERE type = 'working'
    AND status = 'active'
    AND created_at + (half_life_hours * INTERVAL '1 hour') < NOW();

  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END;
$$;

-- =============================================================================
-- full_text_search: tsvector search over memory content
-- =============================================================================

CREATE OR REPLACE FUNCTION full_text_search(p_query TEXT, p_limit INT DEFAULT 10)
RETURNS TABLE (
  id        UUID,
  content   TEXT,
  type      memory_type,
  importance FLOAT,
  rank      FLOAT,
  created_at TIMESTAMPTZ
) LANGUAGE plpgsql AS $$
BEGIN
  RETURN QUERY
  SELECT
    m.id,
    m.content,
    m.type,
    m.importance,
    ts_rank(to_tsvector('english', m.content), plainto_tsquery('english', p_query))::FLOAT AS rank,
    m.created_at
  FROM memories m
  WHERE m.status = 'active'
    AND to_tsvector('english', m.content) @@ plainto_tsquery('english', p_query)
  ORDER BY rank DESC, m.importance DESC
  LIMIT p_limit;
END;
$$;

-- =============================================================================
-- find_causes: recursive CTE causal chain traversal
-- =============================================================================

CREATE OR REPLACE FUNCTION find_causes(p_memory_id UUID, p_depth INT DEFAULT 3)
RETURNS TABLE (
  memory_id    UUID,
  content      TEXT,
  relationship relationship_type,
  depth        INT
) LANGUAGE plpgsql AS $$
BEGIN
  RETURN QUERY
  WITH RECURSIVE causal_chain AS (
    -- Base: direct causes
    SELECT
      mg.memory_id AS mem_id,
      m.content,
      mg.relationship_type,
      1 AS depth
    FROM memory_graph mg
    JOIN memories m ON m.id = mg.memory_id
    WHERE mg.connected_memory_id = p_memory_id
      AND mg.relationship_type IN ('causes', 'caused_by')

    UNION ALL

    -- Recurse
    SELECT
      mg2.memory_id,
      m2.content,
      mg2.relationship_type,
      cc.depth + 1
    FROM causal_chain cc
    JOIN memory_graph mg2 ON mg2.connected_memory_id = cc.mem_id
    JOIN memories m2 ON m2.id = mg2.memory_id
    WHERE cc.depth < p_depth
      AND mg2.relationship_type IN ('causes', 'caused_by')
  )
  SELECT DISTINCT mem_id, content, relationship_type, depth
  FROM causal_chain
  ORDER BY depth, mem_id;
END;
$$;

-- =============================================================================
-- find_contradictions: find memories connected by 'contradicts' edges
-- =============================================================================

CREATE OR REPLACE FUNCTION find_contradictions(p_memory_id UUID)
RETURNS TABLE (
  memory_id  UUID,
  content    TEXT,
  confidence FLOAT
) LANGUAGE plpgsql AS $$
BEGIN
  RETURN QUERY
  SELECT
    CASE WHEN mg.memory_id = p_memory_id
         THEN mg.connected_memory_id
         ELSE mg.memory_id
    END AS memory_id,
    m.content,
    mg.confidence
  FROM memory_graph mg
  JOIN memories m ON m.id = CASE WHEN mg.memory_id = p_memory_id
                                  THEN mg.connected_memory_id
                                  ELSE mg.memory_id END
  WHERE (mg.memory_id = p_memory_id OR mg.connected_memory_id = p_memory_id)
    AND mg.relationship_type = 'contradicts'
    AND m.status = 'active';
END;
$$;

-- =============================================================================
-- hydrate_context: reconstruct full cognitive context from DB
-- Returns a single JSONB blob combining identity, worldview, diary, memories
-- =============================================================================

CREATE OR REPLACE FUNCTION hydrate_context(
  p_query_embedding vector(768),
  p_limit           INT DEFAULT 10
)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
  v_identity   JSONB;
  v_worldview  JSONB;
  v_diary      JSONB;
  v_memories   JSONB;
  v_drives     JSONB;
  v_goals      JSONB;
BEGIN
  -- Identity
  SELECT jsonb_object_agg(key, value ORDER BY priority DESC)
  INTO v_identity FROM identity;

  -- Worldview (top 10 by confidence)
  SELECT jsonb_agg(jsonb_build_object(
    'topic', topic, 'belief', belief, 'confidence', confidence
  ) ORDER BY confidence DESC)
  INTO v_worldview FROM worldview LIMIT 10;

  -- Recent diary (last 5 entries)
  SELECT jsonb_agg(jsonb_build_object(
    'date', date, 'mood', mood, 'entry', entry
  ) ORDER BY date DESC)
  INTO v_diary FROM (SELECT * FROM diary ORDER BY date DESC LIMIT 5) d;

  -- Active drives
  SELECT jsonb_agg(jsonb_build_object(
    'concept', concept, 'level', level, 'source', source
  ))
  INTO v_drives
  FROM drives
  WHERE created_at + (ttl_hours * INTERVAL '1 hour') > NOW();

  -- Active goals
  SELECT jsonb_agg(jsonb_build_object(
    'title', title, 'description', description, 'priority', priority
  ))
  INTO v_goals FROM goals WHERE status = 'active';

  -- Top-K relevant memories (vector similarity)
  IF p_query_embedding IS NOT NULL THEN
    SELECT jsonb_agg(jsonb_build_object(
      'id', id, 'content', content, 'type', type,
      'importance', importance, 'emotional_valence', emotional_valence,
      'created_at', created_at
    ) ORDER BY embedding <=> p_query_embedding)
    INTO v_memories
    FROM (
      SELECT * FROM memories
      WHERE status = 'active' AND embedding IS NOT NULL
      ORDER BY embedding <=> p_query_embedding
      LIMIT p_limit
    ) sub;
  ELSE
    SELECT jsonb_agg(jsonb_build_object(
      'id', id, 'content', content, 'type', type,
      'importance', importance, 'created_at', created_at
    ) ORDER BY importance DESC)
    INTO v_memories
    FROM (
      SELECT * FROM memories WHERE status = 'active'
      ORDER BY importance DESC LIMIT p_limit
    ) sub;
  END IF;

  RETURN jsonb_build_object(
    'identity',  COALESCE(v_identity,  '{}'),
    'worldview', COALESCE(v_worldview, '[]'),
    'diary',     COALESCE(v_diary,     '[]'),
    'drives',    COALESCE(v_drives,    '[]'),
    'goals',     COALESCE(v_goals,     '[]'),
    'memories',  COALESCE(v_memories,  '[]')
  );
END;
$$;
