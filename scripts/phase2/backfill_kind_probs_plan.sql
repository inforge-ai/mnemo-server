-- Backfill 8.3b (full kind distribution, Tom 2026-11-25), step 1 of 3: PLAN. Runs after migration 012.
--   dry run (rolls back):  psql "$DB" -X -f scripts/phase2/backfill_kind_probs_plan.sql
--   record the plan:       psql "$DB" -X -v expected=N -f scripts/phase2/backfill_kind_probs_plan.sql
-- Scope: active atoms with no kind_probs, not already planned. Step 2
-- (backfill_kind_probs_ask.py, in the container) asks the kind question again
-- and keeps the whole distribution; step 3 (backfill_kind_probs_apply.sql)
-- writes kind_probs, and kind / kind_p as its argmax (kind 'unknown' below the
-- kind threshold), so the three stay consistent. Undo restores all three.
-- Atoms are NOT changed here. Counts only.
\set ON_ERROR_STOP on
BEGIN;
CREATE TABLE IF NOT EXISTS repair_kind_probs_2026_12 (
  atom_id        uuid PRIMARY KEY REFERENCES atoms(id) ON DELETE CASCADE,
  agent_id       uuid NOT NULL,
  old_kind       text NOT NULL,
  old_kind_p     double precision,
  old_kind_probs jsonb,
  new_kind       text,              -- filled by step 2; 'unknown' below the kind threshold
  new_kind_p     double precision,  -- the top kind's p
  new_kind_probs jsonb,             -- {"state","plan","observation","rule"}
  model          text,
  cost           numeric,
  error          text,
  planned_at     timestamptz NOT NULL DEFAULT now(),
  asked_at       timestamptz,
  applied_at     timestamptz,
  reverted_at    timestamptz
);
CREATE TEMP TABLE plan ON COMMIT DROP AS
SELECT a.id AS atom_id, a.agent_id, a.kind AS old_kind, a.kind_p AS old_kind_p, a.kind_probs AS old_kind_probs,
       (SELECT name FROM agents WHERE id = a.agent_id) AS agent
FROM atoms a
WHERE a.is_active AND a.kind_probs IS NULL
  AND NOT EXISTS (SELECT 1 FROM repair_kind_probs_2026_12 u WHERE u.atom_id = a.id AND u.reverted_at IS NULL);
SELECT coalesce(agent, 'TOTAL') AS agent, count(*) AS atoms,
       count(*) FILTER (WHERE old_kind = 'unknown') AS now_unknown,
       round(count(*) * 0.0000225, 4) AS est_cost_usd_jev
FROM plan GROUP BY ROLLUP(agent) ORDER BY (agent IS NULL), count(*) DESC;
\if :{?expected}
SELECT (SELECT count(*) FROM plan) = :expected AS ok \gset
\if :ok
INSERT INTO repair_kind_probs_2026_12 (atom_id, agent_id, old_kind, old_kind_p, old_kind_probs)
SELECT atom_id, agent_id, old_kind, old_kind_p, old_kind_probs FROM plan
ON CONFLICT (atom_id) DO UPDATE SET   -- a reverted row is planned afresh
  old_kind = EXCLUDED.old_kind, old_kind_p = EXCLUDED.old_kind_p, old_kind_probs = EXCLUDED.old_kind_probs,
  new_kind = NULL, new_kind_p = NULL, new_kind_probs = NULL, model = NULL, cost = NULL, error = NULL,
  planned_at = now(), asked_at = NULL, applied_at = NULL, reverted_at = NULL;
GRANT SELECT, UPDATE (new_kind, new_kind_p, new_kind_probs, model, cost, error, asked_at)
  ON repair_kind_probs_2026_12 TO mnemo;
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
