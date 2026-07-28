-- ============================================================================
-- reset_decayed_importance.sql
--
-- One-time repair for the decay bug (fixed in 6b8da09).
--
-- The decay_importance() function was computing from created_at every cycle,
-- which re-applied the full since-creation decay factor to already-decayed
-- values. After 500+ heartbeat cycles, 775 memories collapsed to near-zero
-- importance (~1e-40 or smaller). Their original importance is unrecoverable.
--
-- This script resets near-zero memories to a type-based default so they're
-- visible to recall (which filters importance >= 0.3 by default) and Bob's
-- valence pass can re-calibrate them.
--
-- Memories that Bob has already scored (importance >= 0.05) are NOT touched.
--
-- REVIEW BEFORE RUNNING. This is Bob's data.
-- ============================================================================

-- Preview what would change (run this first):
-- SELECT type,
--        COUNT(*) FILTER (WHERE importance < 0.05) AS will_reset,
--        COUNT(*) FILTER (WHERE importance >= 0.05) AS will_keep
-- FROM memories WHERE status='active'
-- GROUP BY type ORDER BY type;

-- The actual reset:
UPDATE memories
SET importance = CASE
    WHEN type = 'episodic'   THEN 0.4   -- events: mid priority, surface for review
    WHEN type = 'semantic'   THEN 0.3   -- facts: lower default, Bob promotes what matters
    WHEN type = 'procedural' THEN 0.5   -- how-to: higher default, usually load-bearing
    WHEN type = 'strategic'  THEN 0.6   -- goals/strategy: high default, always load-bearing
    WHEN type = 'working'    THEN 0.2   -- working: low default, expected to decay fast
    ELSE 0.3
END,
updated_at = NOW()
WHERE status = 'active'
  AND importance < 0.05;   -- only reset the bug-destroyed ones

-- Verify after:
-- SELECT type, COUNT(*), ROUND(AVG(importance)::numeric, 3) AS avg_imp,
--        ROUND(MIN(importance)::numeric, 3) AS min_imp,
--        ROUND(MAX(importance)::numeric, 3) AS max_imp
-- FROM memories WHERE status='active' GROUP BY type ORDER BY type;
