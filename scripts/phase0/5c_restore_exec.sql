-- 5c: restore decayed direct_experience atoms of active non-eval agents.
-- Touches only is_active and restored_at. Requires -v expected=N.
\pset footer off
BEGIN;
WITH upd AS (
  UPDATE atoms a SET is_active = true, restored_at = now()
  FROM agents ag
  WHERE ag.id = a.agent_id
    AND a.is_active = false
    AND a.deactivation_reason = 'decay_or_deleted'
    AND a.source_type = 'direct_experience'
    AND ag.status = 'active'
    AND ag.name NOT LIKE 'locomo-conv-%'
  RETURNING a.id
)
SELECT count(*) AS restored, count(*) = :expected AS ok FROM upd \gset
\echo restored :restored (expected :expected)
\if :ok
  COMMIT;
\else
  \echo MISMATCH - rolling back
  ROLLBACK;
\endif
