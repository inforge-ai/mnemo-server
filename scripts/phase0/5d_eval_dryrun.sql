-- 5d dry run: eval agents (locomo-conv-% plus -v extra list) and what a
-- delete would take, including anything owned by non-eval agents.
\pset footer off
CREATE TEMP TABLE eval_agents AS
  SELECT id, name FROM agents WHERE name LIKE 'locomo-conv-%' OR name IN (:extra);
SELECT e.name, count(a.id) AS atoms FROM eval_agents e LEFT JOIN atoms a ON a.agent_id = e.id
GROUP BY e.name ORDER BY e.name;
SELECT (SELECT count(*) FROM eval_agents) AS agents,
       (SELECT count(*) FROM atoms WHERE agent_id IN (SELECT id FROM eval_agents)) AS atoms,
       (SELECT count(*) FROM capabilities WHERE grantor_id IN (SELECT id FROM eval_agents)
                                            OR grantee_id IN (SELECT id FROM eval_agents)) AS capabilities;
\echo -- rows of NON-eval agents a cascade would remove (all must be 0):
SELECT
  (SELECT count(*) FROM edges e JOIN atoms s ON s.id = e.source_id JOIN atoms t ON t.id = e.target_id
    WHERE (s.agent_id IN (SELECT id FROM eval_agents)) <> (t.agent_id IN (SELECT id FROM eval_agents))) AS cross_edges,
  (SELECT count(*) FROM snapshot_atoms sa JOIN views v ON v.id = sa.view_id JOIN atoms a ON a.id = sa.atom_id
    WHERE v.owner_agent_id NOT IN (SELECT id FROM eval_agents) AND a.agent_id IN (SELECT id FROM eval_agents)) AS foreign_snapshot_rows,
  (SELECT count(*) FROM agent_trust WHERE trusted_sender_uuid IN (SELECT id FROM eval_agents)
                                       AND agent_uuid NOT IN (SELECT id FROM eval_agents)) AS foreign_trust_rows;
