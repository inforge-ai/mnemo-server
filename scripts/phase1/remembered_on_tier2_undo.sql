-- Undo the tier 2 date repair from repair_date_text_2026_11. Restores text,
-- remembered_on and embedding (replace) or verification (flag) on rows still in
-- their repaired state; skips any changed since.
--   psql "$DB" -v expected=N -f scripts/phase1/remembered_on_tier2_undo.sql
-- N = applied, not-yet-reverted rows. Planned-but-unapplied rows are just deleted
-- with: DELETE FROM repair_date_text_2026_11 WHERE applied_at IS NULL;
\set ON_ERROR_STOP on
\if :{?expected}
\else
  \echo 'usage: -v expected=N'
  \quit
\endif
BEGIN;
WITH r1 AS (
  UPDATE atoms a SET text_content = r.old_text, remembered_on = r.old_remembered_on, embedding = r.old_embedding
  FROM repair_date_text_2026_11 r
  WHERE a.id = r.atom_id AND r.action = 'replace' AND r.applied_at IS NOT NULL AND r.reverted_at IS NULL
    AND a.text_content = r.new_text AND a.remembered_on = r.new_remembered_on
  RETURNING a.id),
r2 AS (
  UPDATE atoms a SET verification = r.old_verification
  FROM repair_date_text_2026_11 r
  WHERE a.id = r.atom_id AND r.action = 'flag' AND r.applied_at IS NOT NULL AND r.reverted_at IS NULL
    AND a.verification -> 'flags' ? 'date_suspect'
  RETURNING a.id),
m AS (UPDATE repair_date_text_2026_11 r SET reverted_at = now()
      WHERE r.atom_id IN (SELECT id FROM r1 UNION ALL SELECT id FROM r2) RETURNING 1)
SELECT count(*) = :expected AS ok FROM m \gset
\if :ok
  COMMIT;
  \echo 'reverted' :expected
\else
  \echo 'ABORT: reverted row count differs from expected'
  ROLLBACK;
\endif
