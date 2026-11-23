-- 5c-ter: export (kept_id, absorbed_id) pairs to merge_pairs. Ids only.
-- Requires migration 008 and -v expected=N.
\pset footer off
BEGIN;
WITH ins AS (
  INSERT INTO merge_pairs (absorbed_id, kept_id, agent_id, merged_at)
  SELECT ab.id, l.target_id, ab.agent_id, l.created_at
  FROM access_log l
  JOIN atoms ab ON ab.id = (l.metadata->>'absorbed_atom_id')::uuid
  JOIN agents ag ON ag.id = ab.agent_id
  WHERE l.action = 'merge'
    AND ab.source_type = 'direct_experience' AND ab.deactivation_reason = 'merged'
    AND ag.name NOT LIKE 'locomo-conv-%'
  ON CONFLICT (absorbed_id) DO NOTHING
  RETURNING absorbed_id
)
SELECT count(*) AS exported, count(*) = :expected AS ok FROM ins \gset
\echo exported :exported (expected :expected)
\if :ok
  COMMIT;
\else
  \echo MISMATCH - rolling back
  ROLLBACK;
\endif
