-- Shadow-mode review (docs/phase2-design.md §6). FOR TOM TO RUN HIMSELF.
-- Prints atom text: its output must not go into the repo, logs, commits,
-- reports or any provider.
--
--   psql "postgresql://mnemo_migrator@localhost:5432/mnemo" -X \
--        -v since=2026-12-01 -v seed=0.42 -v out=$HOME/mnemo-review/shadow_verdicts.csv \
--        -f scripts/phase2/review_shadow.sql
--
-- Prints every shadow decision whose final relation is replaces, same or
-- conflict, plus a random 20 of the rest (repeatable with -v seed), and writes
-- a verdict template to -v out (outside the repo): decision_id, final, and an
-- empty `verdict` column. Fill it with `ok` or the correct relation
-- (same | replaces | refines | conflict | unrelated | review).
\set ON_ERROR_STOP on
\if :{?since}
\else
\set since '2026-11-01'
\endif
\if :{?seed}
\else
\set seed 0.42
\endif
\if :{?out}
\else
\echo 'usage: -v out=/path/outside/the/repo/shadow_verdicts.csv'
\quit
\endif
\pset expanded on
\pset format wrapped
BEGIN;
SET LOCAL timezone = 'UTC';
SELECT setseed(:seed);
CREATE TEMP TABLE pick ON COMMIT DROP AS
WITH s AS (
  SELECT d.*, random() AS r FROM relation_decisions d
  WHERE d.mode = 'shadow' AND d.decided_by = 'relation_v2' AND d.created_at >= :'since'::timestamptz
    AND d.final_relation <> 'pending'
)
SELECT * FROM s WHERE final_relation IN ('replaces', 'same', 'conflict')
UNION ALL
(SELECT * FROM s WHERE final_relation NOT IN ('replaces', 'same', 'conflict') ORDER BY r LIMIT 20);

SELECT row_number() OVER (ORDER BY p.final_relation, p.created_at) AS n, p.id AS decision_id,
       (SELECT name FROM agents WHERE id = p.agent_id) AS agent,
       p.final_relation AS final, p.raw_choice AS raw, p.guard_fired AS guard,
       p.inputs->'new'->>'kind' AS new_kind, p.inputs->'old'->>'kind' AS old_kind,
       p.inputs->'new'->>'valid_from' AS new_valid_from, p.inputs->'new'->>'precision' AS new_precision,
       p.inputs->'old'->>'valid_from' AS old_valid_from, p.inputs->'old'->>'precision' AS old_precision,
       p.inputs->'new'->>'origin_class' AS new_origin, p.inputs->'old'->>'origin_class' AS old_origin,
       p.probabilities,
       n.text_content AS new_text, o.text_content AS old_text
FROM pick p
LEFT JOIN atoms n ON n.id = p.new_atom_id
JOIN atoms o ON o.id = p.old_atom_id
ORDER BY p.final_relation, p.created_at;

\pset expanded off
\pset format csv
\pset footer off
\o :out
SELECT id AS decision_id, final_relation AS final, '' AS verdict FROM pick ORDER BY final_relation, created_at;
\o
\echo 'verdict template written to' :out
ROLLBACK;
