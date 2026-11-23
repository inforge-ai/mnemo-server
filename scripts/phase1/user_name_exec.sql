-- Phase 1 deploy: set agents.metadata.user_name from the CSV (see user_name_dryrun.sql).
-- Requires -v csv=/path/to/user_names.csv -v expected=N (the dry run's would_update).
-- Updates active agents only; merges into metadata (other keys kept).
\pset footer off
BEGIN;
CREATE TEMP TABLE un (agent_name text, user_name text) ON COMMIT DROP;
\set copycmd '\\copy un FROM ' :'csv' ' WITH (FORMAT csv)'
:copycmd
WITH upd AS (
  UPDATE agents a
  SET metadata = COALESCE(a.metadata, '{}'::jsonb) || jsonb_build_object('user_name', btrim(u.user_name))
  FROM un u
  WHERE a.name = u.agent_name AND a.status = 'active' AND btrim(u.user_name) <> ''
  RETURNING a.id
)
SELECT count(*) AS updated, count(*) = :expected AS ok FROM upd \gset
\echo updated :updated (expected :expected)
\if :ok
  COMMIT;
\else
  \echo MISMATCH - rolling back
  ROLLBACK;
\endif
