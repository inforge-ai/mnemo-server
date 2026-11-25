-- Backfill 8.1: valid_from / date_precision / date_basis for atoms stored before
-- Phase 2 (date_basis IS NULL). Runs after migration 011. Deterministic; no model.
--   dry run (rolls back):  psql "$DB" -X -f scripts/phase2/backfill_dates.sql
--   execute:               psql "$DB" -X -v expected=N -f scripts/phase2/backfill_dates.sql
--   undo:                  psql "$DB" -X -v expected=N -f scripts/phase2/backfill_dates_undo.sql
-- Rule (spec item 8.1 and G1 change 2):
--   remembered_on NULL                      -> valid_from = created_at, day, basis stated
--   haiku_v1, 1 January 00:00 UTC           -> year  (basis event)
--   haiku_v1, 1st of another month 00:00    -> month (basis event)
--     ... unless the text has an explicit day-and-month date matching that day -> day
--   everything else                         -> day   (basis event), valid_from = remembered_on
-- Every atom (active or not) with date_basis NULL is in scope. Counts only.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL timezone = 'UTC';
CREATE TABLE IF NOT EXISTS repair_dates_2026_12 (
  atom_id            uuid PRIMARY KEY REFERENCES atoms(id) ON DELETE CASCADE,
  old_valid_from     timestamptz,
  old_date_precision text NOT NULL,
  old_date_basis     text,
  new_valid_from     timestamptz NOT NULL,
  new_date_precision text NOT NULL,
  new_date_basis     text NOT NULL,
  applied_at         timestamptz NOT NULL DEFAULT now(),
  reverted_at        timestamptz
);
CREATE TEMP TABLE plan ON COMMIT DROP AS
WITH a AS (
  SELECT x.id, x.agent_id, x.is_active, x.decomposer_version, x.text_content, x.remembered_on, x.created_at,
         x.valid_from, x.date_precision, x.date_basis,
         x.remembered_on IS NOT NULL AND x.remembered_on = date_trunc('day', x.remembered_on) AS midnight,
         (strpos(x.text_content, to_char(x.remembered_on, 'YYYY-MM-DD')) > 0
          OR x.text_content ~* ('\m' || extract(day FROM x.remembered_on)::int || '(st|nd|rd|th)?\s+'
                                || to_char(x.remembered_on, 'Mon') || '[a-z]*\M')
          OR x.text_content ~* ('\m' || to_char(x.remembered_on, 'Mon') || '[a-z]*\.?\s+'
                                || extract(day FROM x.remembered_on)::int || '(st|nd|rd|th)?\M')) AS explicit_day
  FROM atoms x
  WHERE x.date_basis IS NULL
    AND NOT EXISTS (SELECT 1 FROM repair_dates_2026_12 u WHERE u.atom_id = x.id AND u.reverted_at IS NULL)
)
SELECT id AS atom_id, agent_id, is_active, (SELECT name FROM agents WHERE id = a.agent_id) AS agent,
       valid_from AS old_valid_from, date_precision AS old_date_precision, date_basis AS old_date_basis,
       COALESCE(remembered_on, created_at) AS new_valid_from,
       CASE WHEN remembered_on IS NULL THEN 'day'
            WHEN decomposer_version = 'haiku_v1' AND midnight AND extract(day FROM remembered_on) = 1
                 AND NOT explicit_day
              THEN CASE WHEN extract(month FROM remembered_on) = 1 THEN 'year' ELSE 'month' END
            ELSE 'day' END AS new_date_precision,
       CASE WHEN remembered_on IS NULL THEN 'stated' ELSE 'event' END AS new_date_basis
FROM a;

SELECT coalesce(agent, 'TOTAL') AS agent, count(*) AS atoms, count(*) FILTER (WHERE is_active) AS active,
       count(*) FILTER (WHERE new_date_basis = 'stated') AS undated_stated,
       count(*) FILTER (WHERE new_date_precision = 'day' AND new_date_basis = 'event') AS day,
       count(*) FILTER (WHERE new_date_precision = 'month') AS month,
       count(*) FILTER (WHERE new_date_precision = 'year') AS year
FROM plan GROUP BY ROLLUP(agent) ORDER BY (agent IS NULL), count(*) DESC;
-- Undated atoms (G1 change 2): valid_from = created_at, precision day, basis stated — never unknown.
SELECT coalesce(p.agent, 'TOTAL') AS agent, count(*) AS undated,
       count(*) FILTER (WHERE p.new_valid_from = a.created_at AND p.new_date_precision = 'day'
                        AND p.new_date_basis = 'stated') AS valid_from_created_day_stated,
       count(*) FILTER (WHERE NOT (p.new_valid_from = a.created_at AND p.new_date_precision = 'day'
                                   AND p.new_date_basis = 'stated')) AS other,
       count(*) FILTER (WHERE p.new_date_precision = 'unknown') AS precision_unknown
FROM plan p JOIN atoms a ON a.id = p.atom_id
WHERE a.remembered_on IS NULL
GROUP BY ROLLUP(p.agent) ORDER BY (p.agent IS NULL), count(*) DESC;
SELECT to_regclass('repair_remembered_on_2026_11') IS NOT NULL AS has_phase1_repair \gset
\if :has_phase1_repair
SELECT 'phase-1 repaired atoms now on 1 January' AS q, count(*) AS atoms,
       count(*) FILTER (WHERE p.new_date_precision = 'year') AS to_year
FROM plan p JOIN repair_remembered_on_2026_11 r ON r.atom_id = p.atom_id AND r.reverted_at IS NULL
WHERE extract(month FROM p.new_valid_from) = 1 AND extract(day FROM p.new_valid_from) = 1;
\endif

\if :{?expected}
SELECT (SELECT count(*) FROM plan) = :expected AS ok \gset
\if :ok
INSERT INTO repair_dates_2026_12 (atom_id, old_valid_from, old_date_precision, old_date_basis,
                                  new_valid_from, new_date_precision, new_date_basis)
SELECT atom_id, old_valid_from, old_date_precision, old_date_basis, new_valid_from, new_date_precision, new_date_basis
FROM plan;
UPDATE atoms a SET valid_from = p.new_valid_from, date_precision = p.new_date_precision, date_basis = p.new_date_basis
FROM plan p WHERE a.id = p.atom_id;
COMMIT;
\echo 'applied' :expected
\else
\echo 'ABORT: count differs from expected'
ROLLBACK;
\endif
\else
\echo 'dry run only (pass -v expected=N to execute)'
ROLLBACK;
\endif
