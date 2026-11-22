-- Migration 010: sources and provenance (v2 Phase 1)
--
-- Every /remember writes a sources row (the full note plus who wrote it, to
-- whom, and where) before decomposition, so a failed store loses nothing.
-- New atoms point at their source and carry the verbatim supporting quote
-- and the deterministic check results. Existing atoms keep source_id NULL
-- (no backfill). See docs/phase1-design.md §1.

CREATE TABLE IF NOT EXISTS sources (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    -- No FK to store_jobs: pruning job rows must never cascade into sources.
    store_id      UUID NOT NULL,
    text          TEXT NOT NULL,
    speaker       TEXT NOT NULL,
    addressee     TEXT,
    channel       TEXT,
    origin_class  TEXT NOT NULL DEFAULT 'unspecified'
                  CONSTRAINT sources_origin_class_check CHECK (origin_class IN (
                      'user_stated', 'professional', 'measurement',
                      'document', 'agent_inference', 'unspecified')),
    remembered_on TIMESTAMPTZ,
    domain_tags   TEXT[] NOT NULL DEFAULT '{}',   -- request tags, for /retry
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_store ON sources (store_id);
CREATE INDEX IF NOT EXISTS idx_sources_agent ON sources (agent_id, created_at);

ALTER TABLE atoms
    ADD COLUMN IF NOT EXISTS source_id    UUID REFERENCES sources(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS source_quote TEXT,
    ADD COLUMN IF NOT EXISTS verification JSONB;
CREATE INDEX IF NOT EXISTS idx_atoms_source ON atoms (source_id) WHERE source_id IS NOT NULL;

-- A decomposed atom that deduplicated into an existing atom: the existing
-- atom is unchanged; the new source's support for it is recorded here.
CREATE TABLE IF NOT EXISTS source_merges (
    source_id    UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    atom_id      UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    source_quote TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, atom_id)
);
CREATE INDEX IF NOT EXISTS idx_source_merges_atom ON source_merges (atom_id);

-- The source row now holds the text of a failed store; new store_failures
-- rows carry the error only.
ALTER TABLE store_failures ALTER COLUMN original_text DROP NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON sources TO mnemo;
GRANT SELECT, INSERT, DELETE ON source_merges TO mnemo;
