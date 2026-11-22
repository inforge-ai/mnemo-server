-- Phase 1 deploy: set agents.metadata.user_name (preamble "The agent's user is ...").
-- Input: a 2-column CSV (agent_name,user_name), no header, kept OUTSIDE the repo.
-- Run:  psql "postgresql://mnemo_migrator@localhost:5432/mnemo" -v csv=/path/to/user_names.csv -f user_name_dryrun.sql
-- Read-only. Prints agent names and match status, never the user_name values.
\pset footer off
BEGIN;  -- only a temp table is written; always rolled back
CREATE TEMP TABLE un (agent_name text, user_name text) ON COMMIT DROP;
\set copycmd '\\copy un FROM ' :'csv' ' WITH (FORMAT csv)'
:copycmd
SELECT u.agent_name,
       count(a.id) FILTER (WHERE a.status = 'active') AS active_matches,
       count(a.id) AS all_matches,
       bool_or(a.metadata ? 'user_name') AS already_set,
       bool_or(a.metadata->>'user_name' = u.user_name) AS same_value
FROM un u LEFT JOIN agents a ON a.name = u.agent_name
GROUP BY u.agent_name ORDER BY u.agent_name;
SELECT count(*) AS csv_rows,
       (SELECT count(*) FROM agents a JOIN un u ON a.name = u.agent_name AND a.status = 'active') AS would_update
FROM un;
ROLLBACK;
