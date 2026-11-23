-- 5b: classify inactive atoms. Arc atoms that were not merged get 'arc'
-- (Tom, G1). Re-runnable: only touches rows with deactivation_reason NULL.
\pset footer off
BEGIN;
UPDATE atoms a SET deactivation_reason = 'merged'
FROM access_log l
WHERE l.action = 'merge'
  AND (l.metadata->>'absorbed_atom_id')::uuid = a.id
  AND a.is_active = false
  AND a.deactivation_reason IS NULL;
UPDATE atoms SET deactivation_reason = 'arc'
WHERE is_active = false AND deactivation_reason IS NULL AND source_type = 'arc';
UPDATE atoms SET deactivation_reason = 'decay_or_deleted'
WHERE is_active = false AND deactivation_reason IS NULL;
SELECT count(*) AS inactive_unclassified FROM atoms WHERE is_active = false AND deactivation_reason IS NULL;
COMMIT;
SELECT deactivation_reason, source_type, count(*) FROM atoms WHERE NOT is_active GROUP BY 1, 2 ORDER BY 1, 2;
