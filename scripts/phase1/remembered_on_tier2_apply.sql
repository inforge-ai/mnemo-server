-- Tier 2 date repair, step 3 of 3: APPLY the recorded plan in one transaction.
--   psql "$DB" -v expected_replace=N -v expected_flag=M -f scripts/phase1/remembered_on_tier2_apply.sql
-- replace rows: atoms get new_text, new_remembered_on and new_embedding.
-- flag rows:    atoms.verification.flags gets "date_suspect"; nothing else changes.
-- Aborts (rollback) unless every planned row is still in its planned "before"
-- state, every replace row has its new embedding, and the counts match.
\set ON_ERROR_STOP on
\if :{?expected_replace}
\else
  \echo 'usage: -v expected_replace=N -v expected_flag=M'
  \quit
\endif
\if :{?expected_flag}
\else
  \echo 'usage: -v expected_replace=N -v expected_flag=M'
  \quit
\endif
BEGIN;
SET timezone = 'UTC';
SELECT
  (SELECT count(*) FROM repair_date_text_2026_11 r JOIN atoms a ON a.id = r.atom_id
    WHERE r.action = 'replace' AND r.applied_at IS NULL AND r.reverted_at IS NULL
      AND r.new_embedding IS NOT NULL AND a.text_content = r.old_text
      AND a.remembered_on = r.old_remembered_on) = :expected_replace
  AND (SELECT count(*) FROM repair_date_text_2026_11 WHERE action = 'replace' AND applied_at IS NULL AND reverted_at IS NULL) = :expected_replace
  AND (SELECT count(*) FROM repair_date_text_2026_11 r JOIN atoms a ON a.id = r.atom_id
    WHERE r.action = 'flag' AND r.applied_at IS NULL AND r.reverted_at IS NULL
      AND a.remembered_on = r.old_remembered_on
      AND a.verification IS NOT DISTINCT FROM r.old_verification) = :expected_flag
  AND (SELECT count(*) FROM repair_date_text_2026_11 WHERE action = 'flag' AND applied_at IS NULL AND reverted_at IS NULL) = :expected_flag
  AS ok \gset
\if :ok
\else
  \echo 'ABORT: plan rows changed since planning, embeddings missing, or counts differ'
  ROLLBACK;
  \quit
\endif
WITH u AS (
  UPDATE atoms a SET text_content = r.new_text, remembered_on = r.new_remembered_on, embedding = r.new_embedding
  FROM repair_date_text_2026_11 r
  WHERE a.id = r.atom_id AND r.action = 'replace' AND r.applied_at IS NULL AND r.reverted_at IS NULL
    AND a.text_content = r.old_text AND a.remembered_on = r.old_remembered_on
  RETURNING a.id)
UPDATE repair_date_text_2026_11 r SET applied_at = now() FROM u WHERE r.atom_id = u.id;
WITH f AS (
  UPDATE atoms a SET verification = coalesce(a.verification, '{}'::jsonb)
         || jsonb_build_object('flags', coalesce(a.verification -> 'flags', '[]'::jsonb) || '["date_suspect"]'::jsonb)
  FROM repair_date_text_2026_11 r
  WHERE a.id = r.atom_id AND r.action = 'flag' AND r.applied_at IS NULL AND r.reverted_at IS NULL
    AND a.verification IS NOT DISTINCT FROM r.old_verification
  RETURNING a.id)
UPDATE repair_date_text_2026_11 r SET applied_at = now() FROM f WHERE r.atom_id = f.id;
SELECT (SELECT count(*) FROM repair_date_text_2026_11 WHERE action = 'replace' AND applied_at = now()) = :expected_replace
   AND (SELECT count(*) FROM repair_date_text_2026_11 WHERE action = 'flag' AND applied_at = now()) = :expected_flag AS ok \gset
\if :ok
  COMMIT;
  \echo 'applied' :expected_replace 'replace,' :expected_flag 'flag'
\else
  \echo 'ABORT: applied counts differ from expected'
  ROLLBACK;
\endif
