-- Migration 006: Lifecycle relationship edges
--
-- Extends the edge_type allowlist with 'tension_with' and 'narrows';
-- adds nullable metadata JSONB to edges (LLM reasoning, detector version,
-- detection timestamp, cosine-at-detection); adds lifecycle_dlq table
-- for transient Haiku failures so the system degrades gracefully.

ALTER TABLE edges
    DROP CONSTRAINT IF EXISTS edges_edge_type_check;

ALTER TABLE edges
    ADD CONSTRAINT edges_edge_type_check
    CHECK (edge_type IN (
        'supports', 'contradicts', 'depends_on',
        'generalises', 'specialises', 'motivated_by',
        'evidence_for', 'supersedes', 'summarises', 'related',
        'tension_with', 'narrows'
    ));

ALTER TABLE edges
    ADD COLUMN IF NOT EXISTS metadata JSONB;

CREATE TABLE IF NOT EXISTS lifecycle_dlq (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    new_atom_id  UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    candidate_id UUID REFERENCES atoms(id) ON DELETE CASCADE,
    agent_id     UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    error        TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_dlq_agent_created
    ON lifecycle_dlq (agent_id, created_at DESC);

INSERT INTO schema_migrations (version) VALUES ('006_lifecycle_edges') ON CONFLICT DO NOTHING;
