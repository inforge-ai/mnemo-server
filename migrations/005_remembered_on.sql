-- Migration 005: Add remembered_on column for episodic recency ranking
--
-- Ticket 4b (Phase 2). Episodic atoms carry a `remembered_on` timestamp
-- — when the event/observation happened, not when it was stored (that's
-- already created_at). Recall demotes older episodic near-duplicates so
-- "Zulip completed 2026-04-15" outranks "Zulip planned 2026-03-01" on a
-- query about Zulip.
--
-- Nullable by design. Existing atoms stay NULL; the retrieve path falls
-- back to created_at for NULL rows at query time. No backfill — the
-- store self-heals forward as new atoms land with proper timestamps.

ALTER TABLE atoms
    ADD COLUMN remembered_on TIMESTAMPTZ;

-- Partial index for fast within-episodic recency sort. Non-episodic
-- atoms don't use remembered_on, so we skip them to keep the index lean.
CREATE INDEX idx_atoms_episodic_remembered_on
    ON atoms (agent_id, remembered_on DESC)
    WHERE atom_type = 'episodic' AND is_active = true;

INSERT INTO schema_migrations (version) VALUES ('005_remembered_on') ON CONFLICT DO NOTHING;
