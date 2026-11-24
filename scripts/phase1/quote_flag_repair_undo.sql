-- Undo quote_flag_repair.sql: restore alpha, beta and verification from the undo table.
--   psql "$DB" -X -v expected=N -f quote_flag_repair_undo.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT count(*) = :expected AS ok FROM repair_quote_flag_2026_11 WHERE reverted_at IS NULL \gset
\if :ok
UPDATE atoms a SET confidence_alpha = u.old_alpha, confidence_beta = u.old_beta, verification = u.old_verification
FROM repair_quote_flag_2026_11 u WHERE a.id = u.atom_id AND u.reverted_at IS NULL;
UPDATE repair_quote_flag_2026_11 SET reverted_at = now() WHERE reverted_at IS NULL;
COMMIT;
\echo 'reverted' :expected
\else
\echo 'ABORT: count differs from expected'
ROLLBACK;
\endif
