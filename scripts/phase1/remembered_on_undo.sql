-- Undo the remembered_on repair from the undo table. Restores rows whose value is
-- still the repaired one; skips any changed since.
--   psql "$DB" -v expected=N -f scripts/phase1/remembered_on_undo.sql
\set ON_ERROR_STOP on
\if :{?expected}
\else
  \echo 'usage: -v expected=N (rows in repair_remembered_on_2026_11 not yet reverted)'
  \quit
\endif
BEGIN;
WITH u AS (
  UPDATE atoms a SET remembered_on = r.old_remembered_on
  FROM repair_remembered_on_2026_11 r
  WHERE a.id = r.atom_id AND r.reverted_at IS NULL AND a.remembered_on = r.new_remembered_on
  RETURNING r.atom_id),
m AS (UPDATE repair_remembered_on_2026_11 r SET reverted_at = now() FROM u WHERE r.atom_id = u.atom_id RETURNING 1)
SELECT count(*) = :expected AS ok FROM m \gset
\if :ok
  COMMIT;
  \echo 'reverted' :expected
\else
  \echo 'ABORT: reverted row count differs from expected'
  ROLLBACK;
\endif
