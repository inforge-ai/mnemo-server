-- 5d (mnemo_eval): delete every agent NOT on the eval list, so no private
-- memory remains in the eval database. Requires -v extra=...
-- -v expected_agents=N (number of eval agents that must remain).
\pset footer off
BEGIN;
CREATE TEMP TABLE keep ON COMMIT DROP AS
  SELECT id FROM agents WHERE name LIKE 'locomo-conv-%' OR name IN (:extra);
DELETE FROM capabilities
WHERE grantor_id NOT IN (SELECT id FROM keep) OR grantee_id NOT IN (SELECT id FROM keep);
DELETE FROM agents WHERE id NOT IN (SELECT id FROM keep);
-- access_log, decomposer_usage and store_failures/jobs hold non-eval rows
-- with no FK to agents (or text). Clear those for non-eval agents too.
DELETE FROM access_log WHERE agent_id NOT IN (SELECT id FROM keep);
DELETE FROM decomposer_usage WHERE agent_id NOT IN (SELECT id FROM keep);
DELETE FROM operations WHERE agent_id IS NULL OR agent_id NOT IN (SELECT id FROM keep);
SELECT (SELECT count(*) FROM agents) AS agents_left,
       (SELECT count(*) FROM atoms WHERE agent_id NOT IN (SELECT id FROM keep)) AS foreign_atoms
\gset
\echo agents_left :agents_left (expected :expected_agents); non-eval atoms left :foreign_atoms
SELECT :agents_left = :expected_agents AND :foreign_atoms = 0 AS ok \gset
\if :ok
  COMMIT;
\else
  \echo MISMATCH - rolling back
  ROLLBACK;
\endif
SELECT (SELECT count(*) FROM agents) agents, (SELECT count(*) FROM atoms) atoms, (SELECT count(*) FROM edges) edges;
