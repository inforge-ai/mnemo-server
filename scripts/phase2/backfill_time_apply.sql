-- Backfill 8.2, step 3 of 3: APPLY (guarded).  psql "$DB" -X -v expected=N -f scripts/phase2/backfill_time_apply.sql
-- Sets verification.time_unresolved for asked rows. N = asked, not yet applied.
\set ON_ERROR_STOP on
\if :{?expected}
\else
  \echo 'usage: -v expected=N'
  \quit
\endif
BEGIN;
CREATE TEMP TABLE todo ON COMMIT DROP AS
SELECT atom_id, time_unresolved FROM repair_time_2026_12
WHERE asked_at IS NOT NULL AND applied_at IS NULL AND reverted_at IS NULL;
SELECT time_unresolved, count(*) FROM todo GROUP BY ROLLUP(time_unresolved) ORDER BY 1 NULLS LAST;
SELECT (SELECT count(*) FROM todo) = :expected AS ok \gset
\if :ok
UPDATE atoms a SET verification = COALESCE(a.verification, '{}'::jsonb) || jsonb_build_object('time_unresolved', t.time_unresolved)
FROM todo t WHERE a.id = t.atom_id;
UPDATE repair_time_2026_12 u SET applied_at = now() FROM todo t WHERE u.atom_id = t.atom_id;
COMMIT;
\echo 'applied' :expected
\else
\echo 'ABORT: count differs from expected'
ROLLBACK;
\endif
