-- =============================================================================
-- db/07_clustering.sql — HDBSCAN cluster layer + trajectory tracking (Phase 3)
-- =============================================================================
-- Run via:  python scripts/init_db.py --with-age
-- Or manually after 06_age_triggers.sql.
--
-- Safe to re-run: all statements are idempotent.
-- =============================================================================

-- =============================================================================
-- TABLE: memory_clusters
-- One row per HDBSCAN cluster. Outliers (-1) are not stored here.
-- =============================================================================

CREATE TABLE IF NOT EXISTS memory_clusters (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    label           TEXT,                       -- Bob's name (NULL until named)
    hdbscan_label   INT NOT NULL,               -- raw HDBSCAN cluster number
    memory_count    INT NOT NULL DEFAULT 0,
    avg_importance  FLOAT,
    avg_valence     FLOAT,
    centroid_memory_id UUID REFERENCES memories(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_run_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (hdbscan_label)
);

-- =============================================================================
-- TABLE: cluster_trajectory
-- Snapshots cluster avg_importance over time for trend analysis.
-- =============================================================================

CREATE TABLE IF NOT EXISTS cluster_trajectory (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cluster_id      UUID NOT NULL REFERENCES memory_clusters(id) ON DELETE CASCADE,
    avg_importance  FLOAT NOT NULL,
    memory_count    INT NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cluster_trajectory_cluster
    ON cluster_trajectory (cluster_id, recorded_at DESC);

-- =============================================================================
-- TABLE: cluster_membership
-- Many-to-many: memories ↔ clusters. A memory can belong to one cluster
-- (or none, if outlier). Updated on each clustering run.
-- =============================================================================

CREATE TABLE IF NOT EXISTS cluster_membership (
    memory_id       UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    cluster_id      UUID NOT NULL REFERENCES memory_clusters(id) ON DELETE CASCADE,
    distance_to_centroid FLOAT,                -- lower = more representative
    PRIMARY KEY (memory_id, cluster_id)
);

-- =============================================================================
-- FUNCTION: record_cluster_trajectory()
-- Called after each clustering run to snapshot current state.
-- =============================================================================

CREATE OR REPLACE FUNCTION record_cluster_trajectory(p_cluster_id UUID)
RETURNS VOID AS $func$
BEGIN
    INSERT INTO cluster_trajectory (cluster_id, avg_importance, memory_count)
    SELECT
        mc.id,
        mc.avg_importance,
        mc.memory_count
    FROM memory_clusters mc
    WHERE mc.id = p_cluster_id;
END;
$func$ LANGUAGE plpgsql;

-- =============================================================================
-- FUNCTION: get_cluster_trajectory()
-- Returns current + previous avg_importance for a cluster, with days elapsed.
-- =============================================================================

CREATE OR REPLACE FUNCTION get_cluster_trajectory(p_cluster_id UUID)
RETURNS TABLE(
    current_importance FLOAT,
    previous_importance FLOAT,
    days_since_prev FLOAT,
    trend TEXT
) AS $func$
DECLARE
    curr FLOAT;
    prev FLOAT;
    days FLOAT;
BEGIN
    SELECT avg_importance INTO curr
    FROM memory_clusters WHERE id = p_cluster_id;

    SELECT ct.avg_importance, EXTRACT(EPOCH FROM (NOW() - ct.recorded_at)) / 86400.0
    INTO prev, days
    FROM cluster_trajectory ct
    WHERE ct.cluster_id = p_cluster_id
    ORDER BY ct.recorded_at DESC
    LIMIT 1 OFFSET 1;  -- second-most-recent snapshot

    IF prev IS NULL THEN
        RETURN QUERY SELECT curr, NULL::FLOAT, NULL::FLOAT, 'new'::TEXT;
    ELSIF curr > prev THEN
        RETURN QUERY SELECT curr, prev, days, 'rising'::TEXT;
    ELSIF curr < prev THEN
        RETURN QUERY SELECT curr, prev, days, 'declining'::TEXT;
    ELSE
        RETURN QUERY SELECT curr, prev, days, 'stable'::TEXT;
    END IF;
END;
$func$ LANGUAGE plpgsql;

-- =============================================================================
-- FUNCTION: get_representative_memories()
-- Top N memories closest to cluster centroid, ordered by distance.
-- =============================================================================

CREATE OR REPLACE FUNCTION get_representative_memories(
    p_cluster_id UUID,
    p_limit INT DEFAULT 5
)
RETURNS TABLE(
    memory_id UUID,
    content TEXT,
    importance FLOAT,
    emotional_valence FLOAT,
    distance FLOAT
) AS $func$
BEGIN
    RETURN QUERY
    SELECT
        m.id,
        m.content,
        m.importance,
        m.emotional_valence,
        cm.distance_to_centroid
    FROM cluster_membership cm
    JOIN memories m ON m.id = cm.memory_id
    WHERE cm.cluster_id = p_cluster_id
    ORDER BY cm.distance_to_centroid ASC NULLS LAST
    LIMIT p_limit;
END;
$func$ LANGUAGE plpgsql;
