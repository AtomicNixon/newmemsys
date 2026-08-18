-- Session-scoped topic flags for compaction control.
-- Bob flags topics during the session; postcompact.py reads them before
-- falling back to naive extraction.

CREATE TABLE IF NOT EXISTS session_topic_flags (
    session_id  TEXT PRIMARY KEY,
    tags        JSONB NOT NULL DEFAULT '[]'::jsonb,
    note        TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for quick lookup by session
CREATE INDEX IF NOT EXISTS idx_session_topic_flags_updated
    ON session_topic_flags (updated_at);
