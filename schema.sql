-- ============================================================
-- MNEMO v0.3 SCHEMA (operator-scoped auth)
-- ============================================================

-- Operator registry (billing/credential entity)
-- schema_migrations tracking table
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(16) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- platform_config table
CREATE TABLE IF NOT EXISTS platform_config (
    key VARCHAR(64) PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE operators (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                    TEXT NOT NULL UNIQUE,
    email                   TEXT,
    username                TEXT NOT NULL,
    org                     TEXT NOT NULL DEFAULT 'mnemo',
    created_at              TIMESTAMPTZ DEFAULT now(),
    status                  VARCHAR(16) NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'suspended', 'cancelled')),
    stripe_customer_id      VARCHAR(64),
    stripe_subscription_id  VARCHAR(64),
    updated_at              TIMESTAMPTZ DEFAULT now()
);

-- Agent registry (scoped under operators)
CREATE TABLE agents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    operator_id     UUID NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    persona         TEXT,
    domain_tags     TEXT[] NOT NULL DEFAULT '{}',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at  TIMESTAMPTZ,
    status          VARCHAR(16) NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'departed')),
    -- Departure handling
    departed_at     TIMESTAMPTZ,             -- NULL = active, set on departure
    data_expires_at TIMESTAMPTZ,             -- departed_at + 30 days

    CONSTRAINT agents_operator_name_unique UNIQUE (operator_id, name)
);

CREATE INDEX idx_agents_operator ON agents(operator_id);
CREATE INDEX idx_agents_domain_tags ON agents USING GIN (domain_tags);
CREATE INDEX idx_agents_active ON agents (status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_operators_org ON operators(org);
CREATE INDEX IF NOT EXISTS idx_operators_status ON operators(status);

-- Agent addresses (canonical agent_name:operator_username.org)
CREATE TABLE agent_addresses (
    agent_id    UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    address     TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_agent_addresses_address ON agent_addresses (address);

-- Core memory atoms
CREATE TABLE atoms (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,

    -- Type system (assigned by server, not by agent)
    atom_type       TEXT NOT NULL CHECK (atom_type IN (
                        'episodic', 'semantic', 'procedural', 'relational'
                    )),

    -- Content
    text_content    TEXT NOT NULL,
    structured      JSONB DEFAULT '{}',
    embedding       vector(768),             -- google/embeddinggemma-300m

    -- Confidence (inferred by server, stored as Beta distribution)
    confidence_alpha FLOAT NOT NULL DEFAULT 2.0,
    confidence_beta  FLOAT NOT NULL DEFAULT 2.0,

    -- Provenance
    source_type     TEXT NOT NULL DEFAULT 'direct_experience'
                    CHECK (source_type IN (
                        'direct_experience', 'inference', 'shared_view',
                        'imported_skill', 'consolidation', 'arc'
                    )),
    source_ref      UUID,
    derivation      UUID[] DEFAULT '{}',

    -- Domain tagging
    domain_tags     TEXT[] NOT NULL DEFAULT '{}',

    -- Temporal
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed   TIMESTAMPTZ,
    access_count    INTEGER NOT NULL DEFAULT 0,

    -- Decay
    decay_type      TEXT NOT NULL DEFAULT 'exponential'
                    CHECK (decay_type IN ('exponential', 'linear', 'none')),
    decay_half_life_days FLOAT NOT NULL DEFAULT 30.0,

    -- Soft delete
    is_active       BOOLEAN NOT NULL DEFAULT true,

    -- Consolidation tracking (used to skip atoms already processed in recent runs)
    last_consolidated_at TIMESTAMPTZ,

    -- Decomposer provenance
    decomposer_version TEXT NOT NULL DEFAULT 'regex_v1'
);

CREATE INDEX idx_atoms_agent_id ON atoms (agent_id);
CREATE INDEX idx_atoms_agent_type ON atoms (agent_id, atom_type);
CREATE INDEX idx_atoms_domain_tags ON atoms USING GIN (domain_tags);
CREATE INDEX idx_atoms_embedding ON atoms USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
CREATE INDEX idx_atoms_active ON atoms (agent_id, is_active) WHERE is_active = true;

-- Knowledge graph edges
CREATE TABLE edges (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id       UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    target_id       UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    edge_type       TEXT NOT NULL CHECK (edge_type IN (
                        'supports', 'contradicts', 'depends_on',
                        'generalises', 'specialises', 'motivated_by',
                        'evidence_for', 'supersedes', 'summarises', 'related'
                    )),
    weight          FLOAT NOT NULL DEFAULT 1.0
                    CHECK (weight >= 0.0 AND weight <= 1.0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (source_id, target_id, edge_type)
);

CREATE INDEX idx_edges_source ON edges (source_id);
CREATE INDEX idx_edges_target ON edges (target_id);

-- Views (snapshots only for v0.1)
CREATE TABLE views (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_agent_id  UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    alpha           FLOAT NOT NULL DEFAULT 1.0,
    atom_filter     JSONB NOT NULL,
    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_views_owner ON views (owner_agent_id);

-- Snapshot atom cache (frozen atoms at time of snapshot)
-- This ensures the snapshot is immutable even as source atoms decay
CREATE TABLE snapshot_atoms (
    view_id         UUID NOT NULL REFERENCES views(id) ON DELETE CASCADE,
    atom_id         UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    PRIMARY KEY (view_id, atom_id)
);

-- Capabilities (access control)
CREATE TABLE capabilities (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    view_id         UUID NOT NULL REFERENCES views(id) ON DELETE CASCADE,
    grantor_id      UUID NOT NULL REFERENCES agents(id),
    grantee_id      UUID NOT NULL REFERENCES agents(id),
    permissions     TEXT[] NOT NULL DEFAULT '{read}',
    revoked         BOOLEAN NOT NULL DEFAULT false,
    revoked_at      TIMESTAMPTZ DEFAULT NULL,
    parent_cap_id   UUID REFERENCES capabilities(id),
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_capabilities_grantee ON capabilities (grantee_id, revoked);
CREATE INDEX idx_capabilities_view ON capabilities (view_id);

-- Agent trust (directional: agent_uuid trusts trusted_sender_uuid)
CREATE TABLE agent_trust (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_uuid          UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    trusted_sender_uuid UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    note                TEXT,
    UNIQUE(agent_uuid, trusted_sender_uuid),
    CHECK(agent_uuid != trusted_sender_uuid)
);

CREATE INDEX idx_agent_trust_agent ON agent_trust(agent_uuid);

-- Access log (immutable audit trail)
CREATE TABLE access_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id        UUID NOT NULL,
    action          TEXT NOT NULL,
    target_id       UUID,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_access_log_agent ON access_log (agent_id, created_at);

-- API keys (hashed — plaintext never stored; scoped to operators, not agents)
CREATE TABLE api_keys (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id  UUID NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
    key_hash     TEXT NOT NULL,
    key_prefix   TEXT NOT NULL,        -- first 16 chars for display
    name         TEXT DEFAULT 'default',
    created_at   TIMESTAMPTZ DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    is_active    BOOLEAN DEFAULT true
);

CREATE INDEX idx_api_keys_hash     ON api_keys(key_hash);
CREATE INDEX idx_api_keys_operator ON api_keys(operator_id);

-- Operations log (per-call record for remember/recall/recall_shared/export_skill)
CREATE TABLE operations (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id    UUID,           -- authenticated caller (NULL if auth disabled)
    operation   TEXT NOT NULL,  -- 'remember', 'recall', 'recall_shared', 'export_skill'
    target_id   UUID,           -- agent whose memory was acted on
    duration_ms INTEGER,
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_operations_agent ON operations (agent_id, created_at);
CREATE INDEX idx_operations_type  ON operations (operation, created_at);

-- Failed async store jobs (ops visibility, not agent-facing)
CREATE TABLE store_failures (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id    UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    original_text TEXT NOT NULL,
    error       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_store_failures_agent ON store_failures (agent_id, created_at);

GRANT SELECT, INSERT, DELETE ON store_failures TO mnemo;

-- Decomposer token usage (operator cost visibility)
CREATE TABLE decomposer_usage (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    store_id                    UUID NOT NULL,
    operator_id                 UUID NOT NULL,
    agent_id                    UUID NOT NULL,
    model                       TEXT NOT NULL,
    input_tokens                INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER,
    cache_read_input_tokens     INTEGER,
    output_tokens               INTEGER NOT NULL
);

CREATE INDEX idx_decomposer_usage_operator_created
    ON decomposer_usage (operator_id, created_at);

-- ============================================================
-- HELPER FUNCTIONS
-- ============================================================

-- Effective confidence after decay
CREATE OR REPLACE FUNCTION effective_confidence(
    conf_alpha FLOAT,
    conf_beta FLOAT,
    decay_type TEXT,
    decay_half_life_days FLOAT,
    created_at TIMESTAMPTZ,
    last_accessed TIMESTAMPTZ,
    access_count INTEGER
) RETURNS FLOAT AS $$
DECLARE
    age_days FLOAT;
    decay_factor FLOAT;
    base_confidence FLOAT;
    access_bonus FLOAT;
BEGIN
    base_confidence := conf_alpha / (conf_alpha + conf_beta);

    IF decay_type = 'none' THEN
        RETURN base_confidence;
    END IF;

    age_days := EXTRACT(EPOCH FROM (
        now() - COALESCE(last_accessed, created_at)
    )) / 86400.0;

    -- Frequently accessed memories decay slower
    access_bonus := LN(1 + access_count) * 0.1;

    IF decay_type = 'exponential' THEN
        decay_factor := POWER(0.5, age_days / (
            decay_half_life_days * (1.0 + access_bonus)
        ));
    ELSE
        decay_factor := GREATEST(0.0, 1.0 - (age_days / (
            decay_half_life_days * 2.0 * (1.0 + access_bonus)
        )));
    END IF;

    RETURN base_confidence * decay_factor;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Cascade revoke all capabilities granted by a departing agent
CREATE OR REPLACE FUNCTION revoke_agent_capabilities(departing_agent_id UUID)
RETURNS INTEGER AS $$
DECLARE
    revoked_count INTEGER;
BEGIN
    WITH RECURSIVE cap_tree AS (
        -- Direct capabilities granted by this agent
        SELECT id FROM capabilities
        WHERE grantor_id = departing_agent_id AND revoked = false
        UNION
        -- All descendants
        SELECT c.id FROM capabilities c
        JOIN cap_tree ct ON c.parent_cap_id = ct.id
        WHERE c.revoked = false
    )
    UPDATE capabilities SET revoked = true, revoked_at = now()
    WHERE id IN (SELECT id FROM cap_tree);

    GET DIAGNOSTICS revoked_count = ROW_COUNT;
    RETURN revoked_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- GRANTS
-- Run as postgres (superuser). The mnemo app user needs full
-- DML access. access_log is INSERT-only for the app user.
-- ============================================================

GRANT SELECT, INSERT, UPDATE, DELETE
    ON operators, agents, atoms, edges, views, snapshot_atoms, capabilities, agent_addresses, agent_trust
    TO mnemo;

GRANT SELECT, INSERT, UPDATE, DELETE ON platform_config TO mnemo;
GRANT SELECT, INSERT ON schema_migrations TO mnemo;

GRANT SELECT, INSERT, UPDATE, DELETE ON api_keys TO mnemo;

GRANT INSERT, SELECT
    ON access_log
    TO mnemo;

GRANT SELECT, INSERT, DELETE ON operations TO mnemo;

-- Test DB needs DELETE for cleanup; prod only needs SELECT, INSERT
GRANT SELECT, INSERT ON decomposer_usage TO mnemo;

GRANT EXECUTE
    ON FUNCTION effective_confidence(float, float, text, float, timestamptz, timestamptz, integer)
    TO mnemo;

GRANT EXECUTE
    ON FUNCTION revoke_agent_capabilities(uuid)
    TO mnemo;

-- ── Store job tracking ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS store_jobs (
    store_id        UUID PRIMARY KEY,
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    operator_id     UUID NOT NULL REFERENCES operators(id),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'decomposing', 'complete', 'failed')),
    atoms_created   INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_store_jobs_agent ON store_jobs (agent_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON store_jobs TO mnemo;
