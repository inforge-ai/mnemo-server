-- RBAC-Lite (Tier 2): agent keys + share blocking
-- See docs/rbac_lite_spec.md

-- Agent key storage (issued at registration, hashed with SHA-256)
ALTER TABLE agents ADD COLUMN IF NOT EXISTS key_hash TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS key_prefix VARCHAR(20);
CREATE INDEX IF NOT EXISTS idx_agents_key_hash ON agents(key_hash) WHERE key_hash IS NOT NULL;

-- Share blocking (operator blocks inbound shares to their agents)
ALTER TABLE capabilities ADD COLUMN IF NOT EXISTS blocked_by_recipient BOOLEAN NOT NULL DEFAULT FALSE;

-- Track migration
INSERT INTO schema_migrations (version) VALUES ('002') ON CONFLICT DO NOTHING;
