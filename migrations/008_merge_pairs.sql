-- Migration 008: merge_pairs (v2 Phase 0)
--
-- Ids-only record of historical consolidation merges whose absorbed atom is
-- a real memory, kept for Phase 2 (which replaces the dedup/merge logic).
-- kept_id is the surviving atom (access_log.target_id of the 'merge' row);
-- absorbed_id is the deactivated one (metadata->>'absorbed_atom_id').
-- No foreign keys: either atom may later be deleted (e.g. agent purge).
-- Populated by the Phase 0 data window, not by this migration.

CREATE TABLE IF NOT EXISTS merge_pairs (
    absorbed_id  UUID PRIMARY KEY,
    kept_id      UUID NOT NULL,
    agent_id     UUID NOT NULL,
    merged_at    TIMESTAMPTZ NOT NULL,
    exported_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_merge_pairs_kept ON merge_pairs (kept_id);

GRANT SELECT ON merge_pairs TO mnemo;
