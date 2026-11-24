-- Backfill 8.2 (verification.time_unresolved), step 1 of 3: PLAN. Runs after migration 011.
--   dry run:  psql "$DB" -X -f scripts/phase2/backfill_time_plan.sql
--   record:   psql "$DB" -X -v expected=N -f scripts/phase2/backfill_time_plan.sql
-- Candidates: active atoms whose text has a word from the wide relative-time
-- list and no explicit date (ISO or day-and-month), with no time_unresolved
-- answer yet. Step 2 (backfill_time_ask.py, in the container) asks the
-- time_unresolved question; step 3 (backfill_time_apply.sql) writes
-- verification.time_unresolved. Atoms are not changed here. Counts only.
\set ON_ERROR_STOP on
BEGIN;
CREATE TABLE IF NOT EXISTS repair_time_2026_12 (
  atom_id          uuid PRIMARY KEY REFERENCES atoms(id) ON DELETE CASCADE,
  agent_id         uuid NOT NULL,
  old_verification jsonb,
  time_unresolved  boolean,          -- filled by step 2
  p                double precision,
  model            text,
  cost             numeric,
  error            text,
  planned_at       timestamptz NOT NULL DEFAULT now(),
  asked_at         timestamptz,
  applied_at       timestamptz,
  reverted_at      timestamptz
);
CREATE TEMP TABLE plan ON COMMIT DROP AS
SELECT a.id AS atom_id, a.agent_id, a.verification AS old_verification,
       (SELECT name FROM agents WHERE id = a.agent_id) AS agent
FROM atoms a
WHERE a.is_active
  AND a.text_content ~* '\m(today|tomorrow|yesterday|tonight|this morning|next week|last week|this week|last night|next month|last month|soon|recently|currently|now)\M|\m(on|this|next|last)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\M'
  AND NOT a.text_content ~* '\m\d{4}-\d{2}-\d{2}\M|\m(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(st|nd|rd|th)?\M|\m\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\M'
  AND NOT (COALESCE(a.verification, '{}'::jsonb) ? 'time_unresolved')
  AND NOT EXISTS (SELECT 1 FROM repair_time_2026_12 u WHERE u.atom_id = a.id AND u.reverted_at IS NULL);
SELECT coalesce(agent, 'TOTAL') AS agent, count(*) AS candidates, round(count(*) * 0.0000225, 4) AS est_cost_usd_jev
FROM plan GROUP BY ROLLUP(agent) ORDER BY (agent IS NULL), count(*) DESC;
\if :{?expected}
SELECT (SELECT count(*) FROM plan) = :expected AS ok \gset
\if :ok
INSERT INTO repair_time_2026_12 (atom_id, agent_id, old_verification) SELECT atom_id, agent_id, old_verification FROM plan;
GRANT SELECT, UPDATE (time_unresolved, p, model, cost, error, asked_at) ON repair_time_2026_12 TO mnemo;
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
