# Mnemo - Operator-Scoped Auth (Revision of Local Auth Spec)

## For: Claude Code / Implementation Agent
## Status: READY TO BUILD
## Priority: High - production auth model
## Estimated time: 2-3 hours
## Prerequisite: Existing local auth spec partially implemented

---

## Context

The current auth spec uses one API key per agent. This works for
dogfooding but breaks down in production: an operator running 50
agents does not want 50 secrets. The standard pattern (Stripe, AWS,
Supabase) is one credential per operator, agents as resources under
that credential.

This revision adds an operators table, scopes agents under operators,
and uses a single API key per operator to authenticate all agent
operations.

---

## Architecture Change

Before (key-per-agent):
```
API key --> agent
```

After (operator-scoped):
```
API key --> operator --> [agent_1, agent_2, ..., agent_n]
```

The operator is the billing entity, the legal entity that accepts
terms, the human (or organisation) responsible for their agents.
Tom is an operator. Nels is an operator. A Moltboy customer running
50 agents is one operator.

---

## Schema Changes

### New table: operators

```sql
CREATE TABLE operators (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    email       TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    is_active   BOOLEAN DEFAULT true
);
```

### Modify: api_keys now reference operators, not agents

```sql
-- Drop existing FK if present
ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_agent_id_fkey;
ALTER TABLE api_keys RENAME COLUMN agent_id TO operator_id;
ALTER TABLE api_keys ADD CONSTRAINT api_keys_operator_id_fkey
    FOREIGN KEY (operator_id) REFERENCES operators(id) ON DELETE CASCADE;

-- Update index
DROP INDEX IF EXISTS idx_api_keys_agent;
CREATE INDEX idx_api_keys_operator ON api_keys(operator_id);
```

### Modify: agents now belong to an operator

```sql
ALTER TABLE agents ADD COLUMN operator_id UUID REFERENCES operators(id) ON DELETE CASCADE;

-- Name uniqueness is now per-operator, not global
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_name_unique;
ALTER TABLE agents ADD CONSTRAINT agents_operator_name_unique
    UNIQUE (operator_id, name);

CREATE INDEX idx_agents_operator ON agents(operator_id);
```

---

## Registration Flow

### Step 1: Register operator (once)

```
POST /v1/auth/register-operator
{
    "name": "tom-davis",
    "email": "tom@soliton.dev"
}

Response 201:
{
    "operator_id": "abc-123-...",
    "name": "tom-davis",
    "api_key": "mnemo_Kx9mP2rQ...",
    "message": "Save this key - it will not be shown again."
}
```

One operator, one API key. This is the only secret to manage.

### Step 2: Create agents (many, using operator key)

```
POST /v1/agents
Authorization: Bearer mnemo_Kx9mP2rQ...
{
    "name": "analyst-agent",
    "persona": "Equity research specialist",
    "domain_tags": ["finance", "equities"]
}

Response 201:
{
    "agent_id": "def-456-...",
    "name": "analyst-agent",
    "operator_id": "abc-123-..."
}
```

No new API key generated. The agent is a resource under the operator.
The operator's API key is used for all operations on this agent.

### Step 3: Use agent (operator key + agent_id in path)

```
POST /v1/agents/{agent_id}/remember
Authorization: Bearer mnemo_Kx9mP2rQ...
{
    "text": "NVDA earnings beat expectations..."
}
```

The auth layer validates: does this API key belong to an operator
who owns this agent_id? If yes, proceed. If no, 403.

---

## Auth Validation Flow

```python
async def get_current_operator(credentials) -> dict:
    """Validate API key and return operator info."""
    key_hash = hash(credentials.credentials)
    result = await pool.fetchrow("""
        SELECT o.id, o.name, o.is_active
        FROM api_keys ak
        JOIN operators o ON o.id = ak.operator_id
        WHERE ak.key_hash = $1 AND ak.is_active = true
    """, key_hash)
    if not result or not result["is_active"]:
        raise HTTPException(401, "Invalid or inactive API key")
    return dict(result)

async def verify_agent_ownership(operator_id, agent_id) -> dict:
    """Verify operator owns this agent."""
    agent = await pool.fetchrow("""
        SELECT * FROM agents
        WHERE id = $1 AND operator_id = $2 AND is_active = true
    """, agent_id, operator_id)
    if not agent:
        raise HTTPException(403, "Agent not found or not owned by this operator")
    return dict(agent)
```

Applied to every agent endpoint:

```python
@router.post("/agents/{agent_id}/remember")
async def remember(
    agent_id: UUID,
    body: RememberRequest,
    operator=Depends(get_current_operator),
):
    agent = await verify_agent_ownership(operator["id"], agent_id)
    # ... existing logic
```

---

## Auth Endpoints

### POST /v1/auth/register-operator
Creates operator + API key. Returns key once.
If operator name exists: 409 Conflict.

### POST /v1/auth/new-key
Authorization: Bearer mnemo_...
Generates additional API key for the authenticated operator.
Returns new key once. Old key still works.

### GET /v1/auth/me
Authorization: Bearer mnemo_...
Returns operator info: id, name, email, agent count, key prefix.

### POST /v1/agents (modified)
Authorization: Bearer mnemo_...
Creates agent under the authenticated operator.
Name must be unique within this operator (not globally).
Returns agent_id. No API key generated.

### GET /v1/agents (new)
Authorization: Bearer mnemo_...
Lists all agents belonging to the authenticated operator.
Returns array of agent summaries: id, name, persona, atom count.

### DELETE /v1/agents/{agent_id} (modified)
Authorization: Bearer mnemo_...
Departs/deactivates agent. Must be owned by authenticated operator.

---

## MCP Server Changes

The MCP server now needs two things: an operator API key and an
agent identity.

### Environment variables

```
MNEMO_BASE_URL=http://localhost:8000
MNEMO_API_KEY=mnemo_...              # operator's API key
MNEMO_AGENT_NAME=toms-claude-desktop  # agent name (within operator)
```

### Startup flow

```
1. Call GET /v1/auth/me with API key -> get operator_id
2. Call GET /v1/agents?name=MNEMO_AGENT_NAME -> find or create agent
   If agent with this name exists under this operator: use it
   If not: POST /v1/agents to create it
3. Store agent_id for all subsequent tool calls
```

This is idempotent: restart always reconnects to the same agent
because the lookup is (operator_id, agent_name), which is unique.

### Multi-agent MCP (optional parameter on tools)

With operator-scoped auth, the multi-agent MCP becomes natural:

```python
@mcp.tool()
async def remember(text, domain_tags=None, agent_name=None):
    if agent_name:
        # Look up or create agent under this operator
        target = await resolve_agent(agent_name)
    else:
        target = DEFAULT_AGENT_ID
    return await client.remember(agent_id=target, ...)
```

The operator key authenticates. The agent_name routes to the right
identity. No need for the caller to know UUIDs — names are scoped
to the operator so they're unambiguous.

---

## CLI Changes

```bash
# Register as operator (once)
mnemo register-operator "tom-davis" --email tom@soliton.dev
# Returns: operator_id + API key

# Create agents (using MNEMO_API_KEY env var)
export MNEMO_API_KEY=mnemo_...
mnemo create-agent analyst --persona "Equity research" --tags finance
mnemo create-agent coder --persona "Python developer" --tags python
mnemo create-agent clio --persona "Wellness assistant" --tags personal

# List my agents
mnemo list-agents
# Output:
# analyst   (def-456-...)  12 memories  last active 2h ago
# coder     (ghi-789-...)  45 memories  last active 5m ago
# clio      (jkl-012-...)  89 memories  last active 1d ago

# Get new API key for myself
mnemo new-key

# Check who I am
mnemo whoami
# Output:
# Operator: tom-davis
# Agents: 3
# Key prefix: mnemo_Kx9mP2rQ...
```

---

## Migration from Current Auth

For existing agents that have their own API keys:

```python
async def migrate_to_operator_model():
    """One-time migration."""
    # 1. Create operators from distinct api_key holders
    # 2. Move api_keys to reference operators
    # 3. Set operator_id on all agents

    # For dogfood: Tom is one operator, all existing agents are his
    op = await create_operator("tom-davis")

    agents = await pool.fetch("SELECT id FROM agents WHERE is_active = true")
    for agent in agents:
        await pool.execute(
            "UPDATE agents SET operator_id = $1 WHERE id = $2",
            op["id"], agent["id"]
        )

    # Move existing API keys to operator
    await pool.execute(
        "UPDATE api_keys SET operator_id = $1",
        op["id"]
    )

    print(f"Migrated {len(agents)} agents under operator {op['name']}")
```

Run once. All existing agents become Tom's. All existing keys become
operator keys. Everything continues to work.

---

## Feature Flag

`MNEMO_AUTH_ENABLED` (default false) remains.

When false: all endpoints work without auth, operator_id checks
are skipped. Existing tests pass unchanged.

When true: all endpoints require valid operator API key, agent
ownership verified on every call.

---

## Sharing Across Operators

The capability/view system already handles cross-agent sharing.
With operator-scoped auth, sharing works like this:

Agent A (operator Tom) creates a view and grants capability to
Agent B (operator Nels). When Agent B's operator calls recall
through the shared view, the auth layer checks:
1. Does this API key belong to Nels? Yes.
2. Does Nels own Agent B? Yes.
3. Does Agent B have a capability for this view? Yes.
4. Proceed with scoped recall.

No changes needed to the sharing logic. The auth layer just adds
the operator ownership check on top.

---

## What This Enables for Production

A Moltboy customer signs up, registers as an operator, gets one
API key. Creates 50 agents programmatically:

```python
for i in range(50):
    client.create_agent(
        name=f"moltboy-worker-{i}",
        persona="Autonomous task worker",
        domain_tags=["moltboy"]
    )
```

All 50 agents use the same API key. Each has its own memory space.
They can share knowledge via views. The operator sees all their
agents in one dashboard. One invoice, one key, many agents.

---

## Build Order

1. Create operators table (~10 min)
2. Modify api_keys to reference operators (~10 min)
3. Add operator_id to agents table (~10 min)
4. POST /v1/auth/register-operator endpoint (~20 min)
5. Update get_current_agent -> get_current_operator (~20 min)
6. Add verify_agent_ownership dependency (~15 min)
7. Update all agent endpoints with ownership check (~20 min)
8. POST /v1/agents creates under operator (~15 min)
9. GET /v1/agents lists operator's agents (~10 min)
10. Update MCP server startup flow (~20 min)
11. Update CLI commands (~20 min)
12. Migration script (~15 min)
13. Run migration (~5 min)
14. Full regression: pytest tests/ -v (~5 min)

Total estimated: 3 hours

---

## What NOT To Build

- Per-agent API keys (replaced by operator-scoped model)
- Role-based access control (operator owns everything, no roles needed yet)
- Team/organisation hierarchy (operator is flat, one level)
- Usage metering per agent (add with billing, not with auth)
- OAuth flows (API keys are right for machine-to-machine)
