-- 5e: per-agent EXPLAIN ANALYZE of retrieve()'s candidate query
-- (agent filter, active only, ORDER BY cosine distance, LIMIT 20), using the
-- agent's newest active atom's embedding as the query vector. Read-only.
-- Output: agent | rows | execution ms | scan node feeding the sort.
DO $$
DECLARE r record; q vector; j json; plan json; node text;
BEGIN
  FOR r IN SELECT ag.id, ag.name FROM agents ag
           WHERE ag.status = 'active'
             AND EXISTS (SELECT 1 FROM atoms a WHERE a.agent_id = ag.id AND a.is_active)
           ORDER BY ag.name LOOP
    SELECT embedding INTO q FROM atoms WHERE agent_id = r.id AND is_active ORDER BY created_at DESC LIMIT 1;
    EXECUTE 'EXPLAIN (ANALYZE, FORMAT JSON)
             SELECT id, effective_confidence(confidence_alpha, confidence_beta, decay_type,
                    decay_half_life_days, created_at, last_accessed, access_count)
             FROM atoms WHERE agent_id = $1 AND is_active
             ORDER BY embedding <=> $2 LIMIT 20'
      INTO j USING r.id, q;
    plan := j->0->'Plan';
    -- first scan node on atoms (Bitmap Heap Scan / Index Scan / Seq Scan)
    node := jsonb_path_query_first(plan::jsonb,
              'strict $.**."Node Type" ? (@ like_regex "Scan$")') #>> '{}';
    RAISE NOTICE '%|%|%|%', r.name, plan->>'Actual Rows',
      round((j->0->>'Execution Time')::numeric, 1), node;
  END LOOP;
END $$;
