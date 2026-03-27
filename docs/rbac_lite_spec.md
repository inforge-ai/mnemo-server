# Mnemo RBAC-Lite Implementation Spec

## For: Claude Code
## Status: READY TO BUILD
## Priority: Launch blocker — ship by Tuesday 1 April 2026
## Estimated time: 4–6 hours
## Breaking changes: YES — no backward compatibility. Four operators will be migrated manually.

---

## Context

Mnemo currently has Tier 1 auth: operator isolation (Operator A's key can't touch Operator B's data). But within an operator's namespace, all agents are flat — any agent (or anyone with the operator key) can act on any other agent's resources via the REST API.

A security audit by design partner Artis demonstrated: cross-agent atom reads, deletions, injections, self-granted capabilities, and agent departures — all using a single operator key.

This spec implements **RBAC-Lite (Tier 2)**: role-based permission enforcement at the API level with three roles (Admin, Operator, Agent). The goal is to close the worst security holes before the GitHub repo goes public.

The spec describes a migration of agnets from solinet-core to mnemo-net.   This is for 
informational purposes only at this time, do not do the migration.  

---

## 1. Credential Model

Three credential types, each resolving to a role. All are called "keys" for consistency.

| Credential | Header | Env var | Prefix | Role | How issued |
|---|---|---|---|---|---|
| Admin key | `X-Admin-Key` | `MNEMO_ADMIN_KEY` | `mnemo_admin_` | `admin` | Static env var, set at deploy |
| Operator key | `X-Operator-Key` | `MNEMO_OPERATOR_KEY` | `mnemo_op_` | `operator` | Created via `POST /v1/admin/operators` |
| Agent key | `X-Agent-Key` | `MNEMO_AGENT_KEY` | `mnemo_ag_` | `agent` | Issued at agent registration |

All keys are opaque secrets: `{prefix}{base64_random_32_bytes}`. Stored as hashes (argon2id preferred, bcrypt acceptable). Raw key is returned **once** at creation and never stored.

**Rename from current system**: The existing `MNEMO_ADMIN_TOKEN` becomes `MNEMO_ADMIN_KEY`. The existing `MNEMO_API_KEY` / `X-API-Key` becomes `MNEMO_OPERATOR_KEY` / `X-Operator-Key`. The agent key (`MNEMO_AGENT_KEY` / `X-Agent-Key`) is new.

### 1.1 Agent keys (NEW)

When an operator registers an agent (`POST /v1/agents`), the server returns an agent key in addition to the agent UUID and address. This key is the **only** credential that authorises data-plane operations (remember, recall, share, etc.) for that specific agent.

Storage: add `key_hash` column to the agents table (or a separate `agent_keys` table). Hash with argon2id. The raw key is returned once at registration.

### 1.2 Auth resolution

```python
def resolve_auth(request) -> AuthContext:
    # 1. Admin key
    admin_key = request.headers.get("X-Admin-Key")
    if admin_key and verify_admin_key(admin_key):
        return AuthContext(role="admin", operator=None, agent=None)

    # 2. Agent key (data-plane)
    agent_key = request.headers.get("X-Agent-Key")
    if agent_key:
        agent = lookup_agent_by_key(agent_key)
        if agent and agent.status == "active":
            return AuthContext(role="agent", operator=agent.operator, agent=agent)

    # 3. Operator key (management-plane)
    operator_key = request.headers.get("X-Operator-Key")
    if operator_key:
        operator = lookup_operator_by_key(operator_key)
        if operator and operator.status == "active":
            return AuthContext(role="operator", operator=operator, agent=None)

    raise HTTPException(401, "Invalid or missing credentials")
```

**No fallback**: if the wrong key type is sent, the request is rejected with a clear error message ("this endpoint requires an agent key, not an operator key" or vice versa).

---

## 2. Permission Matrix

### Admin (X-Admin-Key) — can do everything

### Operator (X-Operator-Key) — management plane only
- ✓ Register agent (under own namespace)
- ✓ Inspect shares (inbound + outbound for own agents)
- ✓ Block/unblock inbound shares to own agents
- ✗ Create/suspend operators
- ✗ Depart agents (admin only for beta)
- ✗ Delete atoms
- ✗ Remember/recall (agent-level operations)
- ✗ Grant/revoke views
- ✗ Toggle global sharing
- ✗ Rotate own key (admin only)

### Agent (X-Agent-Key) — data plane, own resources only
- ✓ Remember (own atoms only)
- ✓ Recall (own atoms only)
- ✓ Recall shared (only non-blocked inbound capabilities)
- ✓ Grant view (on own atoms only)
- ✓ Revoke own granted views
- ✓ List own shares (inbound + outbound)
- ✓ Stats (own stats only)
- ✗ Register/depart agents
- ✗ Delete atoms
- ✗ Revoke other agents' views
- ✗ Block shares (operator-level action)
- ✗ Any admin operations

---

## 3. Endpoint-Level Enforcement

For each endpoint, enforce role + ownership after auth resolution:

### Admin-only endpoints (require role == "admin")
```
# Launch (Tuesday)
POST   /v1/admin/operators
GET    /v1/admin/operators
POST   /v1/admin/agents/{agent_id}/depart
POST   /v1/admin/agents/{agent_id}/purge            — NEW: hard-delete all atoms + shares, then depart
DELETE /v1/admin/agents/{agent_id}/atoms/{atom_id}
POST   /v1/admin/trust/disable
POST   /v1/admin/trust/enable
DELETE /v1/admin/trust/shares/{capability_id}

# Deferred (post-launch) — handle manually via DB for beta
POST   /v1/admin/operators/{operator_id}/suspend
POST   /v1/admin/operators/{operator_id}/reinstate
POST   /v1/admin/operators/{operator_id}/rotate-key
POST   /v1/admin/agents/{agent_id}/reinstate
```

All path parameters are UUIDs. Operator username and agent address are human-readable labels returned in response bodies but never used in URL paths (username is only unique within an org; address contains a colon which is a URL-reserved character).

### Operator endpoints (require role == "operator" or "admin")
```
POST   /v1/agents                              — register agent under own operator
GET    /v1/operators/me/shares                  — NEW: list shares for own agents
POST   /v1/shares/{capability_id}/block         — NEW: block inbound share
POST   /v1/shares/{capability_id}/unblock       — NEW: unblock inbound share
```

### Agent endpoints (require role == "agent" or "admin")
```
POST   /v1/agents/{agent_id}/remember           — agent_id must match key's agent
GET    /v1/agents/{agent_id}/recall              — agent_id must match key's agent
GET    /v1/agents/{agent_id}/recall_shared       — agent_id must match key's agent
POST   /v1/agents/{agent_id}/share               — agent_id must match key's agent
DELETE /v1/agents/{agent_id}/shares/{cap_id}      — agent_id must match, must be grantor
GET    /v1/agents/{agent_id}/shares              — agent_id must match key's agent
GET    /v1/agents/{agent_id}/stats               — agent_id must match key's agent
```

**Critical enforcement rule**: for every agent endpoint, verify that the agent_id in the URL path matches the agent resolved from the key. This is the core Tier 2 protection — it prevents Agent A from hitting Agent B's endpoints even if they share an operator.

---

## 4. Share Blocking (NEW feature)

### 4.1 Schema change

```sql
ALTER TABLE capabilities ADD COLUMN blocked_by_recipient BOOLEAN NOT NULL DEFAULT FALSE;
```

### 4.2 New endpoints

#### `GET /v1/operators/me/shares`
Requires: operator key. Returns all inbound and outbound shares for agents belonging to this operator.

```json
{
    "inbound": [
        {
            "capability_id": "...",
            "grantor_address": "research-agent:other-operator.acme",
            "grantee_address": "my-agent:me.inforge",
            "view_name": "market-data",
            "atom_count": 15,
            "blocked": false,
            "created_at": "..."
        }
    ],
    "outbound": [
        {
            "capability_id": "...",
            "grantor_address": "my-agent:me.inforge",
            "grantee_address": "their-agent:other.acme",
            "view_name": "project-context",
            "atom_count": 8,
            "blocked": false,
            "created_at": "..."
        }
    ]
}
```

#### `POST /v1/shares/{capability_id}/block`
Requires: operator key. The capability's grantee agent must belong to the authenticated operator.

Sets `blocked_by_recipient = TRUE`.

**Silent**: the grantor is not notified. Their `list_shared` still shows the share as active. The grantee's `recall_shared` simply won't return atoms from this capability.

```json
{
    "capability_id": "...",
    "blocked": true,
    "note": "Inbound share blocked. Agent will no longer see these shared memories."
}
```

#### `POST /v1/shares/{capability_id}/unblock`
Requires: operator key. Same ownership check. Sets `blocked_by_recipient = FALSE`.

### 4.3 Query modification

In the `recall_shared` query, add:

```sql
AND c.blocked_by_recipient = FALSE
```

Zero latency impact — boolean filter on a row already being read.

---

## 5. Agent Purge (NEW admin operation)

### 5.1 Endpoint

#### `POST /v1/admin/agents/{agent_id}/purge`
Requires: admin key. Irreversible. Requires confirmation body: `{"confirm": "purge"}`.

Execution sequence:
1. Revoke all outbound shares (soft-delete capabilities where this agent is grantor)
2. Remove all inbound capabilities (delete capabilities where this agent is grantee)
3. Hard-delete all atoms owned by this agent (DELETE FROM atoms WHERE owner_id = ...)
4. Hard-delete all edges connected to this agent's atoms
5. Depart the agent (set status = "departed")

```json
// Request
POST /v1/admin/agents/550e8400-e29b-41d4-a716-446655440000/purge
{ "confirm": "purge" }

// Response 200
{
    "agent_id": "550e8400-e29b-41d4-a716-446655440000",
    "address": "locomo-session-1:tom.inforge",
    "atoms_deleted": 312,
    "edges_deleted": 1847,
    "shares_revoked": 0,
    "status": "departed"
}
```

### 5.2 Use case

The LoCoMo benchmark agents on mnemo-net have ~1,400 inactive atoms taking up space. Purge each benchmark agent to reclaim storage before public launch. Stats show 2,200 total atoms with only 808 active — the purged atoms are dead weight.

---

## 6. Agent Migration (dev → prod) INFORMATIONAL PURPOSES ONLY, NO MIGRATON FROM DEV->PROD TODAY

### 6.1 Context

Claude Desktop agents (Tom's, Nels's) currently point at solinet-core (dev). Moving to mnemo-net (prod) requires migrating both identity and data.

### 6.2 Procedure

This is a manual process — no new endpoints needed for Tuesday.

**Step 1: Register agent addresses on mnemo-net.**
Use the admin CLI against mnemo-net to create the same operator and agent addresses. The RBAC migration script (Section 8) handles this — it issues new operator keys and agent keys for mnemo-net.

**Step 2: Export atoms from solinet-core.**
```bash
# On solinet-core, dump atoms for a specific agent
psql -U mnemo -d mnemo -c "\copy (
    SELECT a.* FROM atoms a
    JOIN agents ag ON a.owner_id = ag.id
    WHERE ag.address = 'toms-claude:tom.inforge'
    AND a.status = 'active'
) TO '/tmp/toms-claude-atoms.csv' WITH CSV HEADER"
```

Also export edges:
```bash
psql -U mnemo -d mnemo -c "\copy (
    SELECT e.* FROM edges e
    JOIN atoms a ON e.source_id = a.id OR e.target_id = a.id
    JOIN agents ag ON a.owner_id = ag.id
    WHERE ag.address = 'toms-claude:tom.inforge'
) TO '/tmp/toms-claude-edges.csv' WITH CSV HEADER"
```

**Step 3: Remap owner_id.**
The agent UUID on mnemo-net will differ from solinet-core. A small script reads the CSV, replaces the old `owner_id` with the new agent UUID from mnemo-net, and writes updated CSVs.

**Step 4: Import into mnemo-net.**
```bash
# On mnemo-net
psql -U mnemo -d mnemo -c "\copy atoms FROM '/tmp/toms-claude-atoms-remapped.csv' WITH CSV HEADER"
psql -U mnemo -d mnemo -c "\copy edges FROM '/tmp/toms-claude-edges-remapped.csv' WITH CSV HEADER"
```

**Step 5: Update Claude Desktop MCP config.**
Change `MNEMO_BASE_URL` to `https://api.mnemo-ai.com` and `MNEMO_AGENT_KEY` to the new key from mnemo-net.

**Step 6: Verify.**
Run `mnemo_stats` and `mnemo_recall` from Claude Desktop to confirm atoms migrated correctly.

### 6.3 Migrate who?

| Agent | Source | Destination | Action |
|---|---|---|---|
| toms-claude:tom.inforge | solinet-core | mnemo-net | Migrate atoms + new key |
| nels-claude-desktop:nels.inforge | solinet-core | mnemo-net | Migrate atoms + new key |
| LoCoMo benchmark agents | mnemo-net | — | Purge (Section 5) |
| Artis agents | mnemo-net | mnemo-net | New keys only (already on prod) |

---

## 7. Client Interface Changes (mnemo-client)

### 7.1 REST client

The `MnemoClient` (or equivalent) constructor changes:

```python
# For agent operations (remember, recall, share, etc.)
client = MnemoClient(agent_key="mnemo_ag_...", base_url="https://api.mnemo-ai.com")

# For operator operations (register agent, inspect shares, block)
client = MnemoClient(operator_key="mnemo_op_...", base_url="https://api.mnemo-ai.com")

# Both planes in one client
client = MnemoClient(
    agent_key="mnemo_ag_...",
    operator_key="mnemo_op_...",
    base_url="https://api.mnemo-ai.com"
)
```

Method routing:
- `remember()`, `recall()`, `recall_shared()`, `share()`, `revoke_share()`, `list_shared()`, `stats()` → send `X-Agent-Key`
- `register_agent()`, `inspect_shares()`, `block_share()`, `unblock_share()` → send `X-Operator-Key`

If a method is called but the required key wasn't provided, raise immediately: "This operation requires an agent key. Pass agent_key= to MnemoClient."

### 7.2 New client methods

```python
# Operator-level methods (require operator_key)

def inspect_shares(self) -> dict:
    """List all inbound and outbound shares for this operator's agents."""
    # GET /v1/operators/me/shares with X-Operator-Key

def block_share(self, capability_id: str) -> dict:
    """Block an inbound share to one of this operator's agents."""
    # POST /v1/shares/{capability_id}/block with X-Operator-Key

def unblock_share(self, capability_id: str) -> dict:
    """Unblock a previously blocked inbound share."""
    # POST /v1/shares/{capability_id}/unblock with X-Operator-Key
```

### 7.3 MCP server changes

The MCP server reads `MNEMO_AGENT_KEY` from the environment. It sends `X-Agent-Key` on every tool call. Remove all references to `MNEMO_API_KEY`.

**No MCP tool schema changes.** The seven tools stay exactly as they are:
- `mnemo_remember`
- `mnemo_recall`
- `mnemo_recall_shared`
- `mnemo_share`
- `mnemo_revoke_share`
- `mnemo_list_shared`
- `mnemo_stats`

The `agent_id` parameter on each tool becomes redundant (server resolves agent from key). Keep it in the schema. If passed `agent_id` doesn't match the key's agent, return 403.

**No new MCP tools.** `inspect_shares` and `block_share` are operator-level. Operators use the REST API or CLI — not MCP tools.

### 7.4 Claude Desktop MCP config

```json
{
  "mcpServers": {
    "mnemo": {
      "command": "uvx",
      "args": ["mnemo-ai[mcp]"],
      "env": {
        "MNEMO_AGENT_KEY": "mnemo_ag_...",
        "MNEMO_BASE_URL": "https://api.mnemo-ai.com"
      }
    }
  }
}
```

### 7.5 CLI changes

```
# Renamed env vars and flags
MNEMO_ADMIN_TOKEN  →  MNEMO_ADMIN_KEY
--admin-token      →  --admin-key

# New CLI commands for operators
mnemo shares list                           # requires MNEMO_OPERATOR_KEY
mnemo shares block <capability_id>          # requires MNEMO_OPERATOR_KEY
mnemo shares unblock <capability_id>        # requires MNEMO_OPERATOR_KEY
```

Remember to update the pyproject.toml file, bump the project to 0.4.0, rebuild and publish(will need a token, so Tom will have to do that).

---

## 8. Migration Plan (key issuance) (4 operators) 

Manual migration — no automation needed at this scale:

1. Deploy the new server code.
2. Run a one-time script that:
   - Rehashes existing operator keys with `mnemo_op_` prefix (or issue new ones).
   - Generates agent keys for all existing agents.
   - Outputs a table: operator_username → new operator key, agent_address → new agent key.
3. Tom sends each operator their new credentials with updated MCP config instructions.
4. Old `X-API-Key` header stops being accepted immediately.
5. Rename `MNEMO_ADMIN_TOKEN` → `MNEMO_ADMIN_KEY` in server `.env`.

---

## 9. Implementation Sequence

Each step is independently testable:

1. **Schema**: Add `key_hash` column to agents table. Add `blocked_by_recipient` column to capabilities table. Rename admin env var.

2. **Key generation**: Modify agent registration to generate and return an agent key with `mnemo_ag_` prefix. Store only the hash.

3. **Auth middleware**: Rewrite `resolve_auth` to check `X-Admin-Key`, `X-Agent-Key`, `X-Operator-Key` in that order. No fallback.

4. **Endpoint guards**: Add role + ownership checks to every endpoint per Section 3. Start with destructive endpoints.

5. **Share inspection endpoint**: `GET /v1/operators/me/shares`.

6. **Block/unblock endpoints**: `POST /v1/shares/{capability_id}/block` and `/unblock`.

7. **recall_shared filter**: Add `AND NOT blocked_by_recipient` to the query.

8. **Client update**: Update `mnemo-client` constructor to accept `operator_key` and `agent_key`. Update MCP server to read `MNEMO_AGENT_KEY`. Remove all references to `MNEMO_API_KEY` and `X-API-Key`.

9. **Migration script**: Generate new keys for existing operators and agents.

10. **Tests**:
    - Agent key A cannot access Agent B's remember/recall → 403
    - Operator key cannot call remember/recall → 401
    - Agent key cannot register or depart agents → 401
    - Blocked shares don't appear in recall_shared
    - Mismatched agent_id in URL vs key → 403
    - Admin key can do everything
    - Wrong key type on any endpoint → 401 with clear error message
    - Purge deletes all atoms/edges/shares for target agent

11. **Purge endpoint**: `POST /v1/admin/agents/{agent_id}/purge` per Section 5. Test on a LoCoMo agent first.

12. **Purge LoCoMo agents on mnemo-net**: Run purge against each benchmark agent. Verify atom count drops.

13. **Atom migration script**: Write a small script that exports atoms + edges for a given agent from one Postgres instance, remaps owner_id, and imports into another. Per Section 6.

14. **Migrate Claude Desktops**: Export Tom's and Nels's agents from solinet-core, import to mnemo-net, issue new keys, update MCP configs.

---

## 10. What This Does NOT Cover (deferred to post-launch sprint)

- **Operator suspend/reinstate endpoints**: Handle manually via DB for beta. Only 4 operators — if one needs suspending, SSH in and flip the status.
- **Operator key rotation endpoint**: Same — generate a new hash in the DB and message the operator. Build the endpoint when you have enough operators that this is a bottleneck.
- **Agent reinstate endpoint**: If you depart an agent by mistake, fix it in the DB. Reinstate implies undo semantics that you don't need yet.
- **Tier 3 RBAC**: Admin agents vs worker agents, read-only agents, role matrices.
- **JWT / stateless keys**: Keys are opaque secrets validated against the DB.
- **Stripe auto-provisioning**: Manual operator creation via admin CLI.
- **Inbound share approval workflow**: Shares flow through immediately; operators block reactively.
- **Rate limiting**: Per-agent rate limits.

---

## 11. Security Notes

- All keys (admin, operator, agent) are secrets: env vars, not hardcoded, not in git.
- The `blocked_by_recipient` flag is silent by design — prevents information leakage about trust decisions.
- Admin key remains a static env var. Fine for single-admin.
- Key prefixes (`mnemo_admin_`, `mnemo_op_`, `mnemo_ag_`) make leaked credentials greppable and identifiable by type.
