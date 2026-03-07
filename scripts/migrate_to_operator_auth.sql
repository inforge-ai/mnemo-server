-- Migration: key-per-agent → operator-scoped auth
-- Run as postgres superuser against both mnemo and mnemo_test databases.
--
-- Usage:
--   sudo -u postgres psql mnemo -f scripts/migrate_to_operator_auth.sql
--   sudo -u postgres psql mnemo_test -f scripts/migrate_to_operator_auth.sql

BEGIN;

-- 1. Create operators table
CREATE TABLE IF NOT EXISTS operators (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    email       TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    is_active   BOOLEAN DEFAULT true
);

-- 2. Create default operator for existing data
INSERT INTO operators (name, email)
VALUES ('local', NULL)
ON CONFLICT (name) DO NOTHING;

-- 3. Add operator_id to agents (nullable first for migration)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'agents' AND column_name = 'operator_id'
    ) THEN
        ALTER TABLE agents ADD COLUMN operator_id UUID REFERENCES operators(id) ON DELETE CASCADE;
    END IF;
END $$;

-- 4. Assign all existing agents to the local operator
UPDATE agents SET operator_id = (SELECT id FROM operators WHERE name = 'local')
WHERE operator_id IS NULL;

-- 5. Make operator_id NOT NULL
ALTER TABLE agents ALTER COLUMN operator_id SET NOT NULL;

-- 6. Replace global unique name constraint with per-operator unique
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_name_unique;
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_name_key;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'agents_operator_name_unique'
    ) THEN
        ALTER TABLE agents ADD CONSTRAINT agents_operator_name_unique UNIQUE (operator_id, name);
    END IF;
END $$;

-- 7. Index on operator_id
CREATE INDEX IF NOT EXISTS idx_agents_operator ON agents(operator_id);

-- 8. Migrate api_keys: rename agent_id to operator_id, re-point to operators
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'api_keys' AND column_name = 'agent_id'
    ) THEN
        -- Drop old FK and index
        ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_agent_id_fkey;
        DROP INDEX IF EXISTS idx_api_keys_agent;

        -- Rename column
        ALTER TABLE api_keys RENAME COLUMN agent_id TO operator_id;

        -- Point all existing keys to the local operator
        UPDATE api_keys SET operator_id = (SELECT id FROM operators WHERE name = 'local');

        -- Add new FK
        ALTER TABLE api_keys ADD CONSTRAINT api_keys_operator_id_fkey
            FOREIGN KEY (operator_id) REFERENCES operators(id) ON DELETE CASCADE;

        -- New index
        CREATE INDEX IF NOT EXISTS idx_api_keys_operator ON api_keys(operator_id);
    END IF;
END $$;

-- 9. Grant permissions on operators table
GRANT SELECT, INSERT, UPDATE, DELETE ON operators TO mnemo;

COMMIT;
