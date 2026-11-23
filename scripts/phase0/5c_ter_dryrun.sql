-- 5c-ter dry run: merge pairs whose absorbed atom is a non-eval
-- direct_experience atom (these stay inactive; pairs go to merge_pairs).
\pset footer off
SELECT count(*) AS pairs, count(DISTINCT l.target_id) AS distinct_kept,
       count(*) FILTER (WHERE k.is_active) AS kept_active,
       count(*) FILTER (WHERE k.id IS NULL) AS kept_missing
FROM access_log l
JOIN atoms ab ON ab.id = (l.metadata->>'absorbed_atom_id')::uuid
JOIN agents ag ON ag.id = ab.agent_id
LEFT JOIN atoms k ON k.id = l.target_id
WHERE l.action = 'merge'
  AND ab.source_type = 'direct_experience' AND ab.deactivation_reason = 'merged'
  AND ag.name NOT LIKE 'locomo-conv-%';
