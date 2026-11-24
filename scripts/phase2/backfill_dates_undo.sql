-- Undo backfill 8.1.   psql "$DB" -X -v expected=N -f scripts/phase2/backfill_dates_undo.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT count(*) = :expected AS ok FROM repair_dates_2026_12 WHERE reverted_at IS NULL \gset
\if :ok
UPDATE atoms a SET valid_from = u.old_valid_from, date_precision = u.old_date_precision, date_basis = u.old_date_basis
FROM repair_dates_2026_12 u WHERE a.id = u.atom_id AND u.reverted_at IS NULL;
UPDATE repair_dates_2026_12 SET reverted_at = now() WHERE reverted_at IS NULL;
COMMIT;
\echo 'reverted' :expected
\else
\echo 'ABORT: count differs from expected'
ROLLBACK;
\endif
