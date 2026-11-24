-- Backfill 8.3, step 3 of 3: APPLY in one transaction (guarded).
--   psql "$DB" -X -v expected=N -f scripts/phase2/backfill_kind_apply.sql
-- Writes atoms.kind / kind_p from repair_kind_2026_12 for asked rows whose atom
-- still has kind 'unknown' and no kind_p. N = the number of such rows.
\set ON_ERROR_STOP on
\if :{?expected}
\else
  \echo 'usage: -v expected=N'
  \quit
\endif
BEGIN;
CREATE TEMP TABLE todo ON COMMIT DROP AS
SELECT u.atom_id, u.new_kind, u.new_kind_p FROM repair_kind_2026_12 u JOIN atoms a ON a.id = u.atom_id
WHERE u.asked_at IS NOT NULL AND u.applied_at IS NULL AND u.reverted_at IS NULL
  AND a.kind = 'unknown' AND a.kind_p IS NULL;
SELECT new_kind, count(*) FROM todo GROUP BY ROLLUP(new_kind) ORDER BY new_kind NULLS LAST;
SELECT (SELECT count(*) FROM todo) = :expected AS ok \gset
\if :ok
UPDATE atoms a SET kind = t.new_kind, kind_p = t.new_kind_p FROM todo t WHERE a.id = t.atom_id;
UPDATE repair_kind_2026_12 u SET applied_at = now() FROM todo t WHERE u.atom_id = t.atom_id;
COMMIT;
\echo 'applied' :expected
\else
\echo 'ABORT: count differs from expected'
ROLLBACK;
\endif
