-- Guarded repair: shift remembered_on by 1-3 years for TIER 1 candidates only.
--   psql "$DB" -v expected=N -f scripts/phase1/remembered_on_exec.sql
-- N = tier-1 would_update from the dry run. Aborts (rollback) on any mismatch.
-- Old and new values go to repair_remembered_on_2026_11 (the undo table) first.
\set ON_ERROR_STOP on
\if :{?expected}
\else
  \echo 'usage: -v expected=N (tier-1 would_update from the dry run)'
  \quit
\endif
BEGIN;
\ir remembered_on_candidates.sql
CREATE TABLE IF NOT EXISTS repair_remembered_on_2026_11 (
  atom_id            uuid PRIMARY KEY REFERENCES atoms(id) ON DELETE CASCADE,
  agent_id           uuid NOT NULL,
  old_remembered_on  timestamptz NOT NULL,
  new_remembered_on  timestamptz NOT NULL,
  tier               int NOT NULL,
  repaired_at        timestamptz NOT NULL DEFAULT now(),
  reverted_at        timestamptz
);
SELECT count(*) = :expected AS ok FROM cand WHERE tier = 1 \gset
\if :ok
\else
  \echo 'ABORT: tier-1 candidate count differs from expected'
  ROLLBACK;
  \quit
\endif
INSERT INTO repair_remembered_on_2026_11 (atom_id, agent_id, old_remembered_on, new_remembered_on, tier)
SELECT atom_id, agent_id, old_remembered_on, new_remembered_on, tier FROM cand WHERE tier = 1
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
