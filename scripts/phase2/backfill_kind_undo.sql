-- Undo backfill 8.3: restore kind / kind_p from repair_kind_2026_12.
--   psql "$DB" -X -v expected=N -f scripts/phase2/backfill_kind_undo.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT count(*) = :expected AS ok FROM repair_kind_2026_12 WHERE applied_at IS NOT NULL AND reverted_at IS NULL \gset
\if :ok
UPDATE atoms a SET kind = u.old_kind, kind_p = u.old_kind_p
FROM repair_kind_2026_12 u WHERE a.id = u.atom_id AND u.applied_at IS NOT NULL AND u.reverted_at IS NULL;
UPDATE repair_kind_2026_12 SET reverted_at = now() WHERE applied_at IS NOT NULL AND reverted_at IS NULL;
COMMIT;
\echo 'reverted' :expected
\else
\echo 'ABORT: count differs from expected'
ROLLBACK;
\endif
