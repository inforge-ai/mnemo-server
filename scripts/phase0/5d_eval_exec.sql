-- 5d (production): delete eval agents. Capabilities first (no cascade), then
-- agents (atoms, edges, views, snapshot rows cascade) — same order as
-- consolidation._purge_departed_agents. Requires -v extra=...
-- -v expected_agents=N -v expected_atoms=M. Rolls back on any mismatch,
-- including any change to a non-eval agent's atom count.
\pset footer off
BEGIN;
CREATE TEMP TABLE eval_agents ON COMMIT DROP AS
  SELECT id FROM agents WHERE name LIKE 'locomo-conv-%' OR name IN (:extra);
CREATE TEMP TABLE before_counts ON COMMIT DROP AS
  SELECT ag.id, count(a.id) AS n FROM agents ag LEFT JOIN atoms a ON a.agent_id = ag.id
  WHERE ag.id NOT IN (SELECT id FROM eval_agents) GROUP BY ag.id;
SELECT count(*) AS atoms_total_before FROM atoms \gset
SELECT count(*) AS eval_atoms FROM atoms WHERE agent_id IN (SELECT id FROM eval_agents) \gset
DELETE FROM capabilities
WHERE grantor_id IN (SELECT id FROM eval_agents) OR grantee_id IN (SELECT id FROM eval_agents);
WITH del AS (DELETE FROM agents WHERE id IN (SELECT id FROM eval_agents) RETURNING id)
SELECT count(*) AS deleted_agents FROM del \gset
SELECT count(*) AS atoms_total_after FROM atoms \gset
SELECT count(*) AS changed_non_eval FROM before_counts b
  LEFT JOIN (SELECT agent_id, count(*) n FROM atoms GROUP BY 1) c ON c.agent_id = b.id
  WHERE coalesce(c.n, 0) <> b.n \gset
\echo deleted_agents :deleted_agents (expected :expected_agents); eval_atoms :eval_atoms (expected :expected_atoms)
\echo atoms :atoms_total_before -> :atoms_total_after; non-eval agents with changed atom count: :changed_non_eval
SELECT :deleted_agents = :expected_agents
   AND :eval_atoms = :expected_atoms
   AND :atoms_total_before - :atoms_total_after = :eval_atoms
   AND :changed_non_eval = 0 AS ok \gset
\if :ok
  COMMIT;
\else
  \echo MISMATCH - rolling back
  ROLLBACK;
\endif
