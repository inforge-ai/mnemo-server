# Agent Addresses & Sharing MCP Tools — Design Spec

## Status: APPROVED
## Date: 2026-03-10
## Scope: mnemo-server, mnemo-client, mnemo-mcp

---

## Overview

Two features that together enable cross-agent memory sharing:

1. **Agent addresses** — Human-readable identifiers (`agent:operator.org`) replacing bare UUIDs for inter-agent references
2. **Sharing MCP tools** — Three new MCP tools for creating, discovering, and recalling shared knowledge

---

## Part 1: Agent Addresses

### Format

```
{agent_name}:{operator_username}.{operator_org}
```

Uses `:` as separator (URL-safe, no percent-encoding needed in path segments per RFC 3986).

Examples:
- `clio:tom.inforge`
- `nels-claude-desktop:nels.inforge`
- `equity-analyst:tom.inforge`
- `worker-3:acme-corp.moltboy`
- `local:local.mnemo`

### Validation

```
^[a-z0-9]([a-z0-9-]*[a-z0-9])?:[a-z0-9]([a-z0-9-]*[a-z0-9])?\.[a-z0-9]([a-z0-9-]*[a-z0-9])?$
```

- All lowercase (normalize on input)
- Each segment: alphanumeric + hyphens, starts and ends with alphanumeric
- Max total length: 200 characters

### Schema Changes

#### operators table — add columns

```sql
ALTER TABLE operators ADD COLUMN username TEXT;
ALTER TABLE operators ADD COLUMN org TEXT NOT NULL DEFAULT 'mnemo';

-- Backfill existing operators
UPDATE operators SET username = 'nels', org = 'inforge' WHERE name = 'Nels Ylitalo';
UPDATE operators SET username = 'tom', org = 'inforge' WHERE name = 'Tom P. Davis';
UPDATE operators SET username = 'local', org = 'mnemo' WHERE name = 'local';

-- Then enforce NOT NULL and uniqueness (username unique per org, not globally)
ALTER TABLE operators ALTER COLUMN username SET NOT NULL;
ALTER TABLE operators ADD CONSTRAINT operators_username_org_unique UNIQUE (username, org);
```

Validation constraints: `username` matches `^[a-z0-9][a-z0-9-]*$`, `org` same pattern.

#### agent_addresses table — new

```sql
CREATE TABLE agent_addresses (
    agent_id    UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    address     TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Note: UNIQUE constraint on address already creates an implicit unique index.
-- No separate CREATE UNIQUE INDEX needed.
```

Address computed as: `f"{agent.name}:{operator.username}.{operator.org}"`

Populated on agent creation. Backfilled for existing agents.

### Address Resolution

Service function:

```python
async def resolve_address(conn, address: str) -> UUID | None:
    """Resolve agent_name:operator.org to agent UUID."""
    row = await conn.fetchrow(
        "SELECT agent_id FROM agent_addresses WHERE address = $1",
        address.lower()
    )
    return row["agent_id"] if row else None
```

Route helper applied to all endpoints with agent_id in path:

```python
async def resolve_agent_identifier(pool, identifier: str) -> UUID:
    """Accept either UUID or address format.

    Uses the database pool (not a single connection) since this is called
    at the top of route handlers before they acquire their own connection.
    """
    try:
        return UUID(identifier)
    except ValueError:
        agent_id = await resolve_address(pool, identifier)
        if not agent_id:
            raise HTTPException(404, f"Agent not found: {identifier}")
        return agent_id
```

**Critical implementation note:** All existing route handlers declare `agent_id: UUID` in their
FastAPI function signatures. FastAPI validates path parameters against type hints *before* the
handler runs, so an address like `clio:tom.inforge` would fail UUID parsing with a 422 error.

**Fix:** Change all route handler signatures from `agent_id: UUID` to `agent_id: str`, and call
`resolve_agent_identifier()` at the top of each handler. Also update `verify_agent_ownership()`
in `auth.py` to accept `str` and resolve internally. This is mechanical but touches every route file.

Backward compatible — existing UUID-based calls still work.

### New Endpoint

```
GET /v1/agents/resolve/{address}
```

Returns agent info given an address. Auth required (any operator).

Response:
```json
{
    "agent_id": "83fa64d7-...",
    "name": "nels-claude-desktop",
    "address": "nels-claude-desktop:nels.inforge",
    "operator": "Nels Ylitalo"
}
```

404 if not found. Does NOT require caller to own target agent (needed for sharing lookups).

### Agent Info Includes Address

Any response returning agent info includes the `address` field:
- `GET /v1/agents/{agent_id}` — includes `address`
- `GET /v1/agents` (list) — each agent includes `address`
- `GET /v1/agents/{agent_id}/stats` — includes `address`

### Backfill

Run once after deploying. Also runs automatically on agent creation:

```python
async def backfill_addresses():
    agents = await pool.fetch("""
        SELECT a.id, a.name, o.username, o.org
        FROM agents a
        JOIN operators o ON o.id = a.operator_id
        WHERE a.is_active = true
    """)
    for agent in agents:
        address = f"{agent['name']}:{agent['username']}.{agent['org']}"
        await pool.execute("""
            INSERT INTO agent_addresses (agent_id, address)
            VALUES ($1, $2)
            ON CONFLICT (agent_id) DO UPDATE SET address = $2
        """, agent["id"], address.lower())
```

---

## Part 2: Server-Side Changes for Sharing

### New Endpoint: Cross-View Shared Recall

```
POST /v1/agents/{agent_id}/shared_views/recall
```

Searches across ALL views shared with this agent in a single query. Joins `capabilities` -> `snapshot_atoms` -> `atoms` with embedding similarity.

Request body:
```json
{
    "query": "equity earnings analysis",
    "from_agent": "clio:tom.inforge",
    "min_similarity": 0.15,
    "max_results": 5,
    "verbosity": "summary",
    "max_total_tokens": 500
}
```

- `from_agent` (optional): filter by source agent, accepts address or UUID
- Results include source attribution: grantor address, view name
- Scope-safety preserved: query only hits atoms within each view's snapshot
- No graph expansion in cross-view recall (performance; use per-view recall for deep expansion)

**New request model:** `SharedRecallRequest` with fields: `query`, `from_agent`, `min_similarity`, `max_results`, `verbosity`, `max_total_tokens`. Distinct from `RetrieveRequest` which lacks `from_agent`.

**New response fields:** Each atom in the response includes `source_address` (grantor's agent address) and `view_name`. These are joined from `capabilities` → `agent_addresses`.

Response:
```json
{
    "atoms": [
        {
            "id": "...",
            "atom_type": "procedural",
            "text_content": "Always check NII sustainability...",
            "confidence_effective": 0.82,
            "relevance_score": 0.74,
            "source_address": "clio:tom.inforge",
            "view_name": "shared-nels-1710000000"
        }
    ],
    "total_retrieved": 3
}
```

### Modified Endpoint: Query-Based View Creation

```
POST /v1/agents/{agent_id}/views
```

Add optional `query` field to `atom_filter` and `max_atoms` parameter:

```json
{
    "name": "equity-knowledge",
    "description": "Shared with nels: equity analysis",
    "atom_filter": {
        "query": "equity earnings analysis methodology",
        "domain_tags": ["finance"],
        "atom_types": ["semantic", "procedural"],
        "max_atoms": 20
    }
}
```

When `query` is present:
1. Encode query via `embeddings.encode(query)` to get 384-dim vector
2. Apply `domain_tags`/`atom_types` filters first (narrow the candidate set)
3. Run vector similarity search within filtered candidates
4. Take top `max_atoms` results (default 20) and snapshot those atom IDs
5. `max_atoms` is ONLY applied when `query` is present — existing view creation (without `query`) continues to snapshot ALL matching atoms

**New request model:** `ViewCreateRequest.atom_filter` gains optional fields `query: str | None` and `max_atoms: int = 20`.

### Modified Endpoint: Shared Views List

```
GET /v1/agents/{agent_id}/shared_views
```

Already exists but response needs enrichment. Add to each shared view in the response:
- `source_address` — grantor's agent address (joined from `agent_addresses`)
- `granted_at` — when the capability was granted (from `capabilities.created_at`)
- `grantor_id` — UUID of the granting agent

Either extend `ViewResponse` with optional fields or create a new `SharedViewResponse` model.

### Existing Endpoints (No Changes)

- `POST /v1/agents/{agent_id}/grant` — already grants capabilities (MCP `mnemo_share` resolves addresses to UUIDs before calling grant, so `GrantCreate.grantee_id` stays as `UUID`)
- `POST /v1/agents/{agent_id}/shared_views/{view_id}/recall` — kept for targeted single-view recall

---

## Part 3: Client Changes (mnemo-client)

### New Methods

```python
async def resolve_address(self, address: str) -> UUID:
    """Resolve agent address to UUID. Raises MnemoNotFoundError if not found."""

async def recall_all_shared(
    self, agent_id: UUID, query: str,
    from_agent: str | None = None,
    min_similarity: float = 0.15,
    max_results: int = 5,
    verbosity: str = "summary",
    max_total_tokens: int | None = 500,
) -> dict:
    """Recall across all shared views. Calls the cross-view endpoint."""
```

### Modified Methods

- `create_view()` — pass through `query` and `max_atoms` in `atom_filter`

### Error Handling Note

The existing client raises `MnemoAuthError` for both 401 and 403. The MCP tools should catch
`MnemoAuthError` (not a separate `MnemoForbiddenError`) for permission errors.

### Unchanged

- `list_shared_views()`, `grant()`, `recall_shared()` (per-view), `get_agent()` — all stay as-is

---

## Part 4: MCP Tools (mnemo-mcp)

Three new tools, bringing total from 3 to 6.

### mnemo_share

```python
@mcp.tool()
async def mnemo_share(
    query: str,           # What knowledge to share (semantic search)
    share_with: str,      # Target address, e.g. "nels-claude-desktop:nels.inforge"
    name: str | None,     # Optional view name
    domain_tags: list[str] | None,
    agent_id: str | None, # Falls back to DEFAULT_AGENT_ID
) -> str:
```

Flow:
1. Resolve `share_with` address via `client.resolve_address()`
2. Auto-generate view name if not provided: `f"shared-{share_with.split(':')[0]}-{int(time.time())}"`
3. Create query-based view via `client.create_view()` with `atom_filter={"query": query, ...}`
4. Grant read access to resolved agent via `client.grant()`
5. Return confirmation with atom count and view details

### mnemo_list_shared

```python
@mcp.tool()
async def mnemo_list_shared(
    agent_id: str | None,
) -> str:
```

Calls `client.list_shared_views()`. Formats with source addresses, descriptions, atom counts, grant dates.

### mnemo_recall_shared

```python
@mcp.tool()
async def mnemo_recall_shared(
    query: str,
    from_agent: str | None,    # Filter by source address
    max_results: int = 5,
    min_similarity: float = 0.15,
    verbosity: str = "summary",
    max_total_tokens: int | None = 500,
    agent_id: str | None,
) -> str:
```

Calls the new cross-view `client.recall_all_shared()` endpoint. Output wrapped in safety frame with per-result attribution.

---

## Part 5: Testing

### Address Tests (mnemo-server)

- `test_address_format_valid` — `clio:tom.inforge`, `equity-analyst:tom.inforge`, `worker-3:acme-corp.moltboy`
- `test_address_format_invalid` — missing segments, spaces, uppercase normalization
- `test_resolve_address` — create operator+agent, resolve address to UUID
- `test_resolve_address_not_found` — 404 for nonexistent
- `test_address_created_on_agent_creation` — verify agent_addresses populated
- `test_address_in_url_path` — `GET /v1/agents/clio:tom.inforge/stats` works
- `test_uuid_in_url_path_still_works` — backward compatibility

### Sharing Tests (mnemo-server)

- `test_cross_view_recall` — search across multiple shared views in one call
- `test_cross_view_recall_from_agent_filter` — `from_agent` restricts to single source
- `test_query_based_view_creation` — semantic search selects atoms for snapshot
- `test_cross_view_recall_scope_safety` — no atoms leak outside snapshots
- `test_cross_view_recall_attribution` — results include source address and view name

### MCP Tool Tests (mnemo-mcp)

- `test_share_creates_view_and_grants` — end-to-end share flow
- `test_share_invalid_address` — error for nonexistent target
- `test_list_shared_shows_views` — shows granted views with addresses
- `test_list_shared_empty` — "No shared views available."
- `test_recall_shared_returns_attributed_results` — results with `[from ...]`
- `test_recall_shared_from_agent_filter` — filters to single source
- `test_recall_shared_safety_frame` — wrapped in safety markers

---

## Build Order

### Phase 1: Agent Addresses (mnemo-server)
1. Add `username`/`org` columns to operators, backfill
2. Create `agent_addresses` table
3. Address validation utility
4. `resolve_address` service function
5. `resolve_agent_identifier` route helper, apply to all routes
6. Populate address on agent creation
7. Backfill existing agents
8. `GET /v1/agents/resolve/{address}` endpoint
9. Include `address` in agent info responses
10. Address tests
11. Full regression

### Phase 2: Server-Side Sharing (mnemo-server)
1. Query-based atom selection in `create_snapshot` (add `query` to atom_filter)
2. Cross-view shared recall endpoint (`POST /v1/agents/{agent_id}/shared_views/recall`)
3. Cross-view shared recall service function in `view_service.py`
4. Include grantor address in shared view responses
5. Sharing tests
6. Full regression

### Phase 3: Client Updates (mnemo-client)
1. `resolve_address()` method
2. `recall_all_shared()` method
3. Update `create_view()` to pass `query`/`max_atoms`
4. Client tests

### Phase 4: MCP Tools (mnemo-mcp)
1. `mnemo_share` tool
2. `mnemo_list_shared` tool
3. `mnemo_recall_shared` tool
4. MCP tests
5. Full regression across all repos
