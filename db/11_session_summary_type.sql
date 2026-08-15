-- =============================================================================
-- db/11_session_summary_type.sql — Add session_summary to memory_type enum
-- =============================================================================
-- Needed by the compact-interceptor hooks (scripts/hooks/postcompact.py and
-- session_end.py), which save compact/session summaries as memories.
--
-- Safe to re-run: ALTER TYPE ... ADD VALUE IF NOT EXISTS is idempotent.
-- =============================================================================

ALTER TYPE memory_type ADD VALUE IF NOT EXISTS 'session_summary';
