-- 5c-bis: deactivate active consolidation copies of non-eval agents.
-- Requires -v expected=N.
\pset footer off
BEGIN;
WITH upd AS (
  UPDATE atoms a SET is_active = false, deactivation_reason = 'consolidation_copy'
  FROM agents ag
  WHERE ag.id = a.agent_id
    AND a.is_active AND a.source_type = 'consolidation'
    AND ag.name NOT LIKE 'locomo-conv-%'
  RETURNING a.id
)
SELECT count(*) AS deactivated, count(*) = :expected AS ok FROM upd \gset
\echo deactivated :deactivated (expected :expected)
\if :ok
  COMMIT;
\else
  \echo MISMATCH - rolling back
  ROLLBACK;
\endif
