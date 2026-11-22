-- 5c-bis dry run: active consolidation ("Generalised") atoms per non-eval agent.
\pset footer off
SELECT ag.name, count(*) FROM atoms a JOIN agents ag ON ag.id = a.agent_id
WHERE a.is_active AND a.source_type = 'consolidation' AND ag.name NOT LIKE 'locomo-conv-%'
GROUP BY 1 ORDER BY 2 DESC, 1;
SELECT count(*) AS total FROM atoms a JOIN agents ag ON ag.id = a.agent_id
WHERE a.is_active AND a.source_type = 'consolidation' AND ag.name NOT LIKE 'locomo-conv-%';
