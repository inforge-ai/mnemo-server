-- Backfill 8.3 (kind for all active atoms), step 1 of 3: PLAN. Runs after migration 011.
--   dry run (rolls back):  psql "$DB" -X -f scripts/phase2/backfill_kind_plan.sql
--   record the plan:       psql "$DB" -X -v expected=N -f scripts/phase2/backfill_kind_plan.sql
-- Scope: active atoms with kind 'unknown' and no kind_p (never classified),
-- not already in the undo table. Atoms are NOT changed here.
-- Step 2 (backfill_kind_ask.py, in the container) asks the kind question and
-- fills new_kind; step 3 (backfill_kind_apply.sql) writes atoms.kind in one
-- transaction; backfill_kind_undo.sql reverses it. Counts only.
\set ON_ERROR_STOP on
BEGIN;
CREATE TABLE IF NOT EXISTS repair_kind_2026_12 (
  atom_id     uuid PRIMARY KEY REFERENCES atoms(id) ON DELETE CASCADE,
  agent_id    uuid NOT NULL,
  old_kind    text NOT NULL,
  old_kind_p  double precision,
  new_kind    text,                 -- filled by step 2; 'unknown' below the kind threshold
  new_kind_p  double precision,
  kind_top    text,                 -- the model's top kind even below the threshold
  model       text,
  cost        numeric,
  error       text,
  planned_at  timestamptz NOT NULL DEFAULT now(),
  asked_at    timestamptz,
  applied_at  timestamptz,
  reverted_at timestamptz
);
CREATE TEMP TABLE plan ON COMMIT DROP AS
SELECT a.id AS atom_id, a.agent_id, a.kind AS old_kind, a.kind_p AS old_kind_p,
       (SELECT name FROM agents WHERE id = a.agent_id) AS agent
FROM atoms a
WHERE a.is_active AND a.kind = 'unknown' AND a.kind_p IS NULL
  AND NOT EXISTS (SELECT 1 FROM repair_kind_2026_12 u WHERE u.atom_id = a.id AND u.reverted_at IS NULL);
SELECT coalesce(agent, 'TOTAL') AS agent, count(*) AS atoms,
       round(count(*) * 0.0000225, 4) AS est_cost_usd_jev
FROM plan GROUP BY ROLLUP(agent) ORDER BY (agent IS NULL), count(*) DESC;
\if :{?expected}
SELECT (SELECT count(*) FROM plan) = :expected AS ok \gset
\if :ok
INSERT INTO repair_kind_2026_12 (atom_id, agent_id, old_kind, old_kind_p)
SELECT atom_id, agent_id, old_kind, old_kind_p FROM plan;
GRANT SELECT, UPDATE (new_kind, new_kind_p, kind_top, model, cost, error, asked_at) ON repair_kind_2026_12 TO mnemo;
COMMIT;
\echo 'planned' :expected
\else
\echo 'ABORT: plan count differs from expected'
ROLLBACK;
\endif
\else
\echo 'dry run only (pass -v expected=N to record the plan)'
ROLLBACK;
\endif
