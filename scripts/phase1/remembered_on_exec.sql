-- Guarded tier-1 repair: shift remembered_on for the scope only.
--   psql "$DB" -v agents=clio -v which=next -v target=past -v expected=N \
--        -f scripts/phase1/remembered_on_exec.sql
-- N = the dry run's scope count for the same arguments. Aborts (rollback) on any
-- mismatch. Old and new values go to repair_remembered_on_2026_11 (undo table).
\set ON_ERROR_STOP on
\if :{?expected}
\else
  \echo 'usage: -v agents=a,b -v which=same|next|all -v target=nearest|past -v expected=N'
  \quit
\endif
BEGIN;
\ir remembered_on_candidates.sql
\ir remembered_on_scope.sql
SELECT count(*) = :expected AS ok FROM scope \gset
\if :ok
\else
  \echo 'ABORT: scope count differs from expected'
  ROLLBACK;
  \quit
\endif
INSERT INTO repair_remembered_on_2026_11 (atom_id, agent_id, old_remembered_on, new_remembered_on, tier)
SELECT atom_id, agent_id, old_remembered_on, new_remembered_on, tier FROM scope
ON CONFLICT (atom_id) DO UPDATE   -- re-run after an undo
  SET old_remembered_on = EXCLUDED.old_remembered_on, new_remembered_on = EXCLUDED.new_remembered_on,
      tier = EXCLUDED.tier, repaired_at = now(), reverted_at = NULL;
WITH u AS (
  UPDATE atoms a SET remembered_on = r.new_remembered_on
  FROM repair_remembered_on_2026_11 r
  WHERE a.id = r.atom_id AND r.reverted_at IS NULL AND a.remembered_on = r.old_remembered_on
    AND r.repaired_at = now()
  RETURNING 1)
SELECT count(*) = :expected AS ok FROM u \gset
\if :ok
  COMMIT;
  \echo 'repaired' :expected
\else
  \echo 'ABORT: updated row count differs from expected'
  ROLLBACK;
\endif
