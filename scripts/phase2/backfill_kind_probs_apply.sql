-- Backfill 8.3b, step 3 of 3: APPLY in one transaction (guarded).
--   psql "$DB" -X -v expected=N -f scripts/phase2/backfill_kind_probs_apply.sql
-- Writes kind_probs, and kind / kind_p as its argmax, for asked rows whose atom
-- still has no kind_probs (a store or the enforce path may have written one since).
-- N = the number of such rows. Prints how many kinds change.
\set ON_ERROR_STOP on
\if :{?expected}
\else
  \echo 'usage: -v expected=N'
  \quit
\endif
BEGIN;
CREATE TEMP TABLE todo ON COMMIT DROP AS
SELECT u.atom_id, u.new_kind, u.new_kind_p, u.new_kind_probs, a.kind AS cur_kind
FROM repair_kind_probs_2026_12 u JOIN atoms a ON a.id = u.atom_id
WHERE u.asked_at IS NOT NULL AND u.new_kind_probs IS NOT NULL AND u.applied_at IS NULL AND u.reverted_at IS NULL
  AND a.kind_probs IS NULL;
SELECT coalesce(cur_kind, 'TOTAL') AS kind_before, count(*) AS atoms,
       count(*) FILTER (WHERE new_kind = cur_kind) AS same_kind,
       count(*) FILTER (WHERE new_kind <> cur_kind) AS kind_changes,
       count(*) FILTER (WHERE new_kind = 'unknown') AS now_unknown
FROM todo GROUP BY ROLLUP(cur_kind) ORDER BY cur_kind NULLS LAST;
SELECT (SELECT count(*) FROM todo) = :expected AS ok \gset
\if :ok
UPDATE atoms a SET kind = t.new_kind, kind_p = t.new_kind_p, kind_probs = t.new_kind_probs
FROM todo t WHERE a.id = t.atom_id;
UPDATE repair_kind_probs_2026_12 u SET applied_at = now() FROM todo t WHERE u.atom_id = t.atom_id;
COMMIT;
\echo 'applied' :expected
\else
\echo 'ABORT: count differs from expected'
ROLLBACK;
\endif
