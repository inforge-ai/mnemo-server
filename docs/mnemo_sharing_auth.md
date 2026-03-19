# Mnemo Sharing Auth Spec

**Status:** Draft
**Author:** Tom Davis
**Date:** 2026-03-19

## Problem

Mnemo's agent-to-agent sharing (`mnemo_share` / `mnemo_recall_shared`) currently has no trust gating. Any agent with API access can share memories to any other agent, and those memories are returned on `mnemo_recall_shared` without restriction. For LLMs, **reading is the attack surface** — prompt injection occurs the moment untrusted content enters the context window. The gate must sit *before* content is recalled, not at the crypto layer.

### Threat Model

| Threat | Vector | Impact |
|---|---|---|
| Prompt injection via shared memory | Rogue agent shares crafted atoms to a target agent | Target agent executes injected instructions on next `recall_shared` |
| Spam / noise | Compromised API key used to flood agents with shares | Noise in recall results, storage bloat |
| Rogue internal agent | A same-org agent behaves unexpectedly | Unwanted content enters trusted agents' context windows |

### Design Principles

- Mnemo is a memory layer, **not a communication bus**. No handshakes, no bidirectional negotiation.
- Sharing remains fire-and-forget from the sender's perspective.
- Trust decisions are **unilateral** — the recipient (or its operator) decides.
- Trust is a first-class Mnemo concept, not an external auth layer. It is the source-level counterpart to atom-level confidence: confidence answers "how much should I trust this knowledge?", trust answers "how much should I trust the source of this knowledge?"
- Trust management is an **operator action**, not an agent action. Trust mutation is never exposed as an MCP tool — it lives in the CLI only, outside any LLM's tool inventory.

---

## Design

### Trust Table

Each agent has explicit rows listing which other agents it trusts as senders. No wildcards, no policy engine — just rows.

```sql
CREATE TABLE agent_trust (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_uuid UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    trusted_sender_uuid UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    note TEXT,  -- optional, e.g. "design partner, added 2026-04-01"
    UNIQUE(agent_uuid, trusted_sender_uuid)
);

CREATE INDEX idx_agent_trust_agent ON agent_trust(agent_uuid);
```

Trust is directional: a row `(agent_uuid=A, trusted_sender_uuid=B)` means A will recall shared memories from B. It says nothing about whether B trusts A.

### Auto-Seeding on Agent Creation

When a new agent is created, trust rows are inserted symmetrically for all existing agents in the same org:

```sql
-- On creation of new_agent_uuid in org 'inforge':
INSERT INTO agent_trust (agent_uuid, trusted_sender_uuid)
SELECT :new_agent_uuid, id FROM agents WHERE org = :org AND id != :new_agent_uuid
UNION ALL
SELECT id, :new_agent_uuid FROM agents WHERE org = :org AND id != :new_agent_uuid;
```

This means same-org agents trust each other by default. The rows are explicit — no fallback logic, no "if empty assume same-org" branching. If trust needs to be revoked for a specific agent, delete the rows. No exception syntax needed.

### `recall_shared` Query Change

Add a join on `agent_trust` so only memories from trusted senders are returned:

```sql
-- Add to the existing recall_shared query:
JOIN agent_trust at ON at.agent_uuid = :receiving_agent_uuid
                   AND at.trusted_sender_uuid = shared_memories.sender_agent_uuid
```

If an agent has zero trust entries (edge case — new agent in an empty org), `recall_shared` returns nothing. Secure by default.

Shared memories from untrusted senders remain in the database. They are not deleted, just not surfaced. If trust is later granted, they become visible.

### `list_shared` Metadata Change

The existing `mnemo_list_shared` MCP tool should include a `trusted` boolean field in its response, derived from whether the sender appears in the receiving agent's trust list. This lets operators (and chief-of-staff agents) see what's pending from unknown senders without recalling the content.

```json
{
    "share_id": "uuid",
    "sender_agent_uuid": "uuid",
    "sender_agent_address": "scout:nels.partnercorp",
    "atom_count": 3,
    "shared_at": "2026-03-19T14:00:00Z",
    "trusted": false
}
```

No content is returned for untrusted shares. The agent sees the envelope, not the letter.

### CLI Commands

Trust management is CLI-only. Not registered as MCP tools.

```bash
# List an agent's current trust list
mnemo admin trust list --agent astraea:tom.inforge

# Add trust (unidirectional: agent trusts sender)
mnemo admin trust add --agent astraea:tom.inforge --trusts scout:nels.partnercorp

# Add trust symmetrically (both directions)
mnemo admin trust add --agent astraea:tom.inforge --trusts scout:nels.partnercorp --mutual

# Remove trust
mnemo admin trust remove --agent astraea:tom.inforge --trusts rogue:tom.inforge

# Remove trust from ALL agents for a rogue agent (both directions)
mnemo admin trust revoke --agent rogue:tom.inforge

# List all untrusted pending shares for an agent
mnemo admin trust inbox --agent astraea:tom.inforge
```

These call the same service layer that the MCP tools and API use. They're just not in the MCP tool registry. Implementation: Click commands in the `mnemo-ai` package under a `mnemo admin` subgroup.

---

## Scope Boundaries

**In scope:**
- `agent_trust` table and migration
- Auto-seeding logic on agent creation
- `recall_shared` query change (trust join)
- `list_shared` response addition (`trusted` field)
- CLI commands for trust management

**Explicitly out of scope (see `mnemo_sharing_auth_future.md`):**
- Wildcard / glob trust policies
- Admin roles or permission hierarchy
- MCP tools for trust mutation
- Ed25519 signing / signature verification
- Rate limiting on shares
- Capability-scoped trust (`trust_scope`)
- Trust-as-geometry (Beta distribution on trust relationships)

---

## Open Questions

1. **TTL on untrusted shares.** Should untrusted shared memories expire after N days? Prevents unbounded storage from spam, but means a late trust grant could miss historical shares. Recommendation: defer, revisit if storage becomes an issue.

2. **Backfill for existing shared memories.** Tom ↔ Nels shares predate the trust table. The migration should seed trust rows for existing agent pairs, making current shares retroactively visible. Verify this with a test against the existing shared_memories data.

3. **Agent deletion cascade.** `ON DELETE CASCADE` on both foreign keys means deleting an agent cleans up its trust rows automatically. Verify this doesn't conflict with existing shared_memories cascade behaviour.
