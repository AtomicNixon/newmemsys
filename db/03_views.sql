-- =============================================================================
-- Memory System: Views v1.0
-- =============================================================================

-- v_health: overall system health snapshot
CREATE OR REPLACE VIEW v_health AS
SELECT
  count(*)                                          AS total_memories,
  count(*) FILTER (WHERE status = 'active')        AS active,
  count(*) FILTER (WHERE status = 'expired')       AS expired,
  count(*) FILTER (WHERE status = 'archived')      AS archived,
  count(*) FILTER (WHERE status = 'deleted')       AS deleted,
  count(*) FILTER (WHERE type = 'episodic')        AS episodic,
  count(*) FILTER (WHERE type = 'semantic')        AS semantic,
  count(*) FILTER (WHERE type = 'procedural')      AS procedural,
  count(*) FILTER (WHERE type = 'strategic')       AS strategic,
  count(*) FILTER (WHERE type = 'working')         AS working,
  round(avg(importance)::numeric, 4)               AS avg_importance,
  round(avg(emotional_valence)::numeric, 4)        AS avg_emotional_valence,
  max(created_at)                                  AS newest_memory_at,
  now() - max(created_at)                          AS newest_memory_age,
  (SELECT count(*) FROM diary)                     AS diary_entries,
  (SELECT count(*) FROM identity)                  AS identity_keys,
  (SELECT count(*) FROM goals WHERE status = 'active') AS active_goals,
  (SELECT count(*) FROM memory_graph)              AS graph_edges,
  (SELECT count(*) FROM outbox WHERE status = 'pending') AS pending_outbox
FROM memories;

-- v_active_goals: currently active goals
CREATE OR REPLACE VIEW v_active_goals AS
SELECT id, title, description, priority, source, deadline, created_at
FROM goals
WHERE status = 'active'
ORDER BY
  CASE priority
    WHEN 'critical' THEN 1
    WHEN 'high'     THEN 2
    WHEN 'normal'   THEN 3
    WHEN 'low'      THEN 4
  END,
  created_at;

-- v_active_drives: drives that have not expired
CREATE OR REPLACE VIEW v_active_drives AS
SELECT id, concept, level, source, ttl_hours, created_at,
       (created_at + (ttl_hours * INTERVAL '1 hour')) AS expires_at
FROM drives
WHERE created_at + (ttl_hours * INTERVAL '1 hour') > NOW()
ORDER BY level DESC;

-- v_recent_memories: last 20 active memories for quick narrative
CREATE OR REPLACE VIEW v_recent_memories AS
SELECT id, type, content, importance, emotional_valence, tags, created_at
FROM memories
WHERE status = 'active'
ORDER BY created_at DESC
LIMIT 20;

-- v_high_importance: important memories at risk of decay
CREATE OR REPLACE VIEW v_high_importance AS
SELECT id, type, content, importance, half_life_hours,
       created_at,
       created_at + (half_life_hours * INTERVAL '1 hour') AS expires_at
FROM memories
WHERE status = 'active'
  AND importance >= 0.7
ORDER BY importance DESC;
