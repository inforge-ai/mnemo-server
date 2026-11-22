-- 5c dry run: per non-eval active agent, inactive direct_experience atoms
-- split by merged (stay inactive) / decayed (restore candidates).
\pset footer off
SELECT ag.name,
       count(*) FILTER (WHERE a.deactivation_reason = 'decay_or_deleted') AS decayed_restore,
       count(*) FILTER (WHERE a.deactivation_reason = 'merged')           AS merged_stay
FROM atoms a JOIN agents ag ON ag.id = a.agent_id
WHERE NOT a.is_active AND a.source_type = 'direct_experience'
  AND ag.status = 'active' AND ag.name NOT LIKE 'locomo-conv-%'
GROUP BY ag.name ORDER BY 2 DESC, 1;
SELECT count(*) FILTER (WHERE a.deactivation_reason = 'decay_or_deleted') AS total_decayed_restore,
       count(*) FILTER (WHERE a.deactivation_reason = 'merged')           AS total_merged_stay
FROM atoms a JOIN agents ag ON ag.id = a.agent_id
WHERE NOT a.is_active AND a.source_type = 'direct_experience'
  AND ag.status = 'active' AND ag.name NOT LIKE 'locomo-conv-%';
