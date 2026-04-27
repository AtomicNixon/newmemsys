-- =============================================================================
-- Heartbeat: log table + runtime config keys
-- =============================================================================

CREATE SEQUENCE IF NOT EXISTS heartbeat_cycle_seq START 1;

CREATE TABLE IF NOT EXISTS heartbeat_log (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  cycle_number   INT NOT NULL DEFAULT nextval('heartbeat_cycle_seq'),
  started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at   TIMESTAMPTZ,
  energy_used    INT NOT NULL DEFAULT 0,
  tasks_run      JSONB NOT NULL DEFAULT '[]',
  diary_entry_id UUID REFERENCES diary(id) ON DELETE SET NULL,
  notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_heartbeat_log_started ON heartbeat_log (started_at DESC);

-- Runtime config keys (upsert — safe to re-run)
-- Ground state is YES. The consent queue handles anything needing human eyes.
-- The daemon runs automatically after init. Bob controls his own systems.
INSERT INTO heartbeat_config (key, value) VALUES
  ('enabled',           'true'),
  ('energy_budget',     '50'),
  ('energy_current',    '50'),
  ('energy_last_refill', to_jsonb(now()::text)),
  ('last_run',          'null'),
  ('next_run',          'null'),
  ('cycle_count',       '0'),
  ('chat_model',        '"qwen3.5:latest"')
ON CONFLICT (key) DO NOTHING;

-- Both gates open by default
UPDATE heartbeat_config SET value = 'true'       WHERE key = 'is_configured';
UPDATE heartbeat_config SET value = 'true'        WHERE key = 'enabled';
UPDATE heartbeat_config SET value = '"4x_daily"'  WHERE key = 'frequency';
-- Raise energy budget to 50 (idempotent — also sets current to 50 on fresh install)
INSERT INTO heartbeat_config (key, value) VALUES ('energy_budget', '50')
  ON CONFLICT (key) DO UPDATE SET value = '50';

