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
-- Operators with no agents left (api_keys cascade from operators).
BEGIN;
WITH del AS (
  DELETE FROM operators o
  WHERE NOT EXISTS (SELECT 1 FROM agents a WHERE a.operator_id = o.id)
  RETURNING o.id
)
SELECT count(*) AS deleted_operators FROM del \gset
\echo deleted_operators :deleted_operators
COMMIT;

SELECT (SELECT count(*) FROM agents) agents, (SELECT count(*) FROM atoms) atoms,
       (SELECT count(*) FROM edges) edges, (SELECT count(*) FROM operators) operators,
       (SELECT count(*) FROM api_keys) api_keys;

-- Rewrite every table so deleted rows (memory text) are physically gone,
-- not just dead tuples. VACUUM cannot run inside a transaction.
VACUUM FULL;
