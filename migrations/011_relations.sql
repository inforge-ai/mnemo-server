-- Migration 011: relations decided on write (v2 Phase 2)
--
-- atoms: kind, validity, date precision/basis, and status (kept in step with
-- is_active by a trigger, so the existing is_active code paths need no change:
-- archived <=> NOT is_active; superseded and merged stay is_active and are
-- hidden only when recall_hide_superseded is true).
-- edges: who decided an edge, and retirement (edges are never deleted).
-- relation_decisions: one row per pair asked (shadow and enforce).
-- conflicts: open/closed conflicts between two atoms.
-- See docs/phase2-design.md §1.

ALTER TABLE atoms
    ADD COLUMN IF NOT EXISTS kind           TEXT NOT NULL DEFAULT 'unknown'
        CONSTRAINT atoms_kind_check CHECK (kind IN ('state', 'plan', 'observation', 'rule', 'unknown')),
    ADD COLUMN IF NOT EXISTS kind_p         DOUBLE PRECISION
        CONSTRAINT atoms_kind_p_check CHECK (kind_p >= 0 AND kind_p <= 1),
    ADD COLUMN IF NOT EXISTS status         TEXT NOT NULL DEFAULT 'active'
        CONSTRAINT atoms_status_check CHECK (status IN ('active', 'superseded', 'merged', 'archived')),
    ADD COLUMN IF NOT EXISTS valid_from     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_until    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS date_precision TEXT NOT NULL DEFAULT 'unknown'
        CONSTRAINT atoms_date_precision_check CHECK (date_precision IN ('day', 'month', 'year', 'unknown')),
    ADD COLUMN IF NOT EXISTS date_basis     TEXT
        CONSTRAINT atoms_date_basis_check CHECK (date_basis IN ('event', 'stated'));

UPDATE atoms SET status = 'archived' WHERE NOT is_active AND status <> 'archived';

ALTER TABLE atoms DROP CONSTRAINT IF EXISTS atoms_status_is_active;
ALTER TABLE atoms ADD CONSTRAINT atoms_status_is_active CHECK ((status = 'archived') = (NOT is_active));

CREATE INDEX IF NOT EXISTS idx_atoms_agent_status ON atoms (agent_id, status) WHERE is_active;

-- Keep status and is_active in step. is_active wins when both change.
CREATE OR REPLACE FUNCTION atoms_sync_status() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT NEW.is_active THEN
            NEW.status := 'archived';
        ELSIF NEW.status = 'archived' THEN
            NEW.is_active := false;
        END IF;
    ELSIF NEW.is_active IS DISTINCT FROM OLD.is_active THEN
        IF NOT NEW.is_active THEN
            NEW.status := 'archived';
        ELSIF NEW.status = 'archived' THEN
            NEW.status := 'active';
        END IF;
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        NEW.is_active := (NEW.status <> 'archived');
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS atoms_sync_status ON atoms;
CREATE TRIGGER atoms_sync_status BEFORE INSERT OR UPDATE OF is_active, status ON atoms
    FOR EACH ROW EXECUTE FUNCTION atoms_sync_status();

-- edges
ALTER TABLE edges
    ADD COLUMN IF NOT EXISTS decided_by TEXT
        CONSTRAINT edges_decided_by_check CHECK (decided_by IN
            ('legacy_lifecycle', 'relation_v2', 'relation_v2_backfill', 'agent')),
    ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ;

UPDATE edges SET decided_by = 'legacy_lifecycle'
WHERE edge_type IN ('supersedes', 'tension_with', 'narrows') AND decided_by IS NULL;

ALTER TABLE edges DROP CONSTRAINT IF EXISTS edges_edge_type_check;
ALTER TABLE edges ADD CONSTRAINT edges_edge_type_check CHECK (edge_type IN (
    'supports', 'contradicts', 'depends_on', 'generalises', 'specialises',
    'motivated_by', 'evidence_for', 'supersedes', 'summarises', 'related',
    'tension_with', 'narrows',
    'same', 'replaces', 'refines', 'conflict', 'review'));

-- One row per pair asked.
CREATE TABLE IF NOT EXISTS relation_decisions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id       UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    -- NULL only in shadow mode when the legacy 0.97 dedup merged the new atom
    new_atom_id    UUID REFERENCES atoms(id) ON DELETE CASCADE,
    old_atom_id    UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    source_id      UUID,
    model          TEXT,
    probabilities  JSONB,
    inputs         JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_choice     TEXT,
    final_relation TEXT NOT NULL
        CONSTRAINT relation_decisions_final_check CHECK (final_relation IN
            ('same', 'replaces', 'refines', 'conflict', 'unrelated', 'review', 'pending')),
    guard_fired    TEXT,
    mode           TEXT NOT NULL CONSTRAINT relation_decisions_mode_check CHECK (mode IN ('shadow', 'enforce')),
    decided_by     TEXT NOT NULL
        CONSTRAINT relation_decisions_decided_by_check CHECK (decided_by IN ('relation_v2', 'relation_v2_backfill')),
    edge_id        UUID REFERENCES edges(id) ON DELETE SET NULL,
    attempts       INT NOT NULL DEFAULT 1,
    latency_ms     INT,
    cost           NUMERIC,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_rd_new ON relation_decisions (new_atom_id);
CREATE INDEX IF NOT EXISTS idx_rd_old ON relation_decisions (old_atom_id);
CREATE INDEX IF NOT EXISTS idx_rd_agent_created ON relation_decisions (agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rd_pending ON relation_decisions (created_at) WHERE final_relation = 'pending';

CREATE TABLE IF NOT EXISTS conflicts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id            UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    atom_a              UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    atom_b              UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    decision_id         UUID REFERENCES relation_decisions(id) ON DELETE SET NULL,
    opened_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_by_atom_id UUID REFERENCES atoms(id) ON DELETE SET NULL,
    resolved_at         TIMESTAMPTZ,
    CONSTRAINT conflicts_distinct CHECK (atom_a <> atom_b)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_conflicts_open
    ON conflicts (LEAST(atom_a, atom_b), GREATEST(atom_a, atom_b)) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_conflicts_agent_open ON conflicts (agent_id) WHERE resolved_at IS NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON relation_decisions, conflicts TO mnemo;
