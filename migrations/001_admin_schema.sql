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
INSERT INTO platform_config (key, value) VALUES ('sharing_enabled', 'true'::jsonb) ON CONFLICT DO NOTHING;

-- Operators: is_active → status + stripe columns + updated_at
ALTER TABLE operators ADD COLUMN IF NOT EXISTS status VARCHAR(16);
UPDATE operators SET status = CASE WHEN is_active THEN 'active' ELSE 'suspended' END WHERE status IS NULL;
ALTER TABLE operators ALTER COLUMN status SET NOT NULL;
ALTER TABLE operators ALTER COLUMN status SET DEFAULT 'active';
ALTER TABLE operators ADD CONSTRAINT operators_status_check CHECK (status IN ('active', 'suspended', 'cancelled'));
ALTER TABLE operators DROP COLUMN IF EXISTS is_active;
ALTER TABLE operators ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(64);
ALTER TABLE operators ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(64);
ALTER TABLE operators ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- Agents: is_active → status
ALTER TABLE agents ADD COLUMN IF NOT EXISTS status VARCHAR(16);
UPDATE agents SET status = CASE WHEN is_active THEN 'active' ELSE 'departed' END WHERE status IS NULL;
ALTER TABLE agents ALTER COLUMN status SET NOT NULL;
ALTER TABLE agents ALTER COLUMN status SET DEFAULT 'active';
ALTER TABLE agents ADD CONSTRAINT agents_status_check CHECK (status IN ('active', 'departed'));
ALTER TABLE agents DROP COLUMN IF EXISTS is_active;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_operators_org ON operators(org);
CREATE INDEX IF NOT EXISTS idx_operators_status ON operators(status);

-- Grants
GRANT SELECT, INSERT, UPDATE, DELETE ON platform_config TO mnemo;
GRANT SELECT, INSERT ON schema_migrations TO mnemo;
