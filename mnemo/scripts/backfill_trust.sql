-- Backfill agent_trust for existing capability grants.
-- Run ONCE when deploying the trust feature to a database with existing shares.
--
-- Seeds trust rows so that existing shared views remain visible under the new
-- trust-gated recall. Only creates grantee→grantor trust (the direction needed
-- for recall_shared to work). Does NOT create the reverse direction — use
-- `mnemo admin trust add --mutual` if bidirectional trust is desired.
--
-- Safe to run multiple times (ON CONFLICT DO NOTHING).

-- 1. Create the table if it doesn't exist yet
CREATE TABLE IF NOT EXISTS agent_trust (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_uuid          UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    trusted_sender_uuid UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    note                TEXT,
    UNIQUE(agent_uuid, trusted_sender_uuid),
    CHECK(agent_uuid != trusted_sender_uuid)
);

CREATE INDEX IF NOT EXISTS idx_agent_trust_agent ON agent_trust(agent_uuid);

-- 2. Grant permissions
GRANT SELECT, INSERT, UPDATE, DELETE ON agent_trust TO mnemo;

-- 3. Backfill from existing active capabilities
INSERT INTO agent_trust (agent_uuid, trusted_sender_uuid, note)
SELECT DISTINCT c.grantee_id, c.grantor_id, 'backfill: pre-existing capability grant'
FROM capabilities c
WHERE c.revoked = false
  AND c.grantee_id != c.grantor_id
ON CONFLICT DO NOTHING;

-- 4. Also seed same-org trust for all existing agents (mirrors auto-seed on creation)
INSERT INTO agent_trust (agent_uuid, trusted_sender_uuid, note)
SELECT a1.id, a2.id, 'backfill: same-org auto-seed'
FROM agents a1
JOIN operators o1 ON o1.id = a1.operator_id
JOIN agents a2 ON a2.id != a1.id
JOIN operators o2 ON o2.id = a2.operator_id AND o2.org = o1.org
WHERE a1.is_active = true AND a2.is_active = true
ON CONFLICT DO NOTHING;
