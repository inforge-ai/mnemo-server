-- Migration 007: deactivation tracking + no self-loop edges (v2 Phase 0)
--
-- deactivation_reason records why an atom is inactive ('merged',
-- 'decay_or_deleted', 'arc', 'consolidation_copy', ...). restored_at marks
-- atoms reactivated by the Phase 0 restore.
-- Applied by `mnemo migrate` in a single transaction.

ALTER TABLE atoms
    ADD COLUMN IF NOT EXISTS deactivation_reason TEXT,
    ADD COLUMN IF NOT EXISTS restored_at TIMESTAMPTZ;

-- Self-loop edges carry no information; production has a few hundred.
DELETE FROM edges WHERE source_id = target_id;

ALTER TABLE edges DROP CONSTRAINT IF EXISTS edges_no_self_loop;
ALTER TABLE edges
    ADD CONSTRAINT edges_no_self_loop CHECK (source_id <> target_id);
