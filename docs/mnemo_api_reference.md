# Mnemo API Reference

Base URL: `http://localhost:8000/v1` (default)

All endpoints are prefixed with `/v1`. When authentication is enabled, include `Authorization: Bearer <api_key>` on all requests.

---

## Authentication

### Register Operator

Create an operator account and receive an API key.

```
POST /auth/register-operator
```

**Request Body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Operator display name |
| `email` | string | no | Contact email |
| `username` | string | no | Username (used in agent addresses) |
| `org` | string | no | Organization (default: `"mnemo"`) |

**Response** `201`

```json
{
  "operator_id": "uuid",
  "name": "my-operator",
  "api_key": "mnemo_abc123...",
  "message": "Store this API key — it will not be shown again."
}
```

---

### Generate New Key

Generate an additional API key for the authenticated operator.

```
POST /auth/new-key
```

**Response** `200`

```json
{
  "operator_id": "uuid",
  "api_key": "mnemo_def456...",
  "message": "Store this API key — it will not be shown again."
}
```

---

### Get Current Operator

```
GET /auth/me
```

**Response** `200` — Operator info including agent count.

---

## Agents

### Register Agent

```
POST /agents
```

**Request Body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Agent name |
| `persona` | string | no | Description of the agent's role |
| `domain_tags` | string[] | no | Topic tags (e.g. `["python", "devops"]`) |
| `metadata` | object | no | Arbitrary key-value metadata |

**Response** `201`

```json
{
  "id": "uuid",
  "name": "my-agent",
  "persona": "python developer",
  "domain_tags": ["python"],
  "metadata": {},
  "created_at": "2026-03-15T10:00:00Z",
  "is_active": true,
  "address": "my-agent:operator.mnemo"
}
```

---

### List Agents

```
GET /agents
GET /agents?name=my-agent
```

**Query Parameters**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Filter by exact name |

**Response** `200` — Array of agent objects.

---

### Get Agent

```
GET /agents/{agent_id}
```

**Response** `200` — Single agent object.

---

### Resolve Agent Address

Resolve a human-readable agent address to its UUID.

```
GET /agents/resolve/{address}
```

**Example:** `GET /agents/resolve/my-agent:operator.mnemo`

**Response** `200`

```json
{
  "agent_id": "uuid"
}
```

---

### Get Agent Stats

```
GET /agents/{agent_id}/stats
```

**Response** `200`

```json
{
  "agent_id": "uuid",
  "total_atoms": 142,
  "active_atoms": 130,
  "atoms_by_type": {"episodic": 45, "semantic": 60, "procedural": 25},
  "arc_atoms": 12,
  "total_edges": 87,
  "avg_effective_confidence": 0.72,
  "active_views": 3,
  "granted_capabilities": 2,
  "received_capabilities": 1,
  "address": "my-agent:operator.mnemo"
}
```

---

### Depart Agent

Mark an agent as departed. Cascade-revokes all capabilities the agent granted. Agent data is retained for 30 days before deletion.

```
POST /agents/{agent_id}/depart
```

**Response** `200`

```json
{
  "capabilities_revoked": 3,
  "departed_at": "2026-03-15T10:00:00Z",
  "data_expires_at": "2026-04-14T10:00:00Z"
}
```

---

## Memory

### Remember

Store a memory from free text. The server automatically decomposes the text into typed atoms, assigns confidence scores, detects duplicates, and creates graph edges.

Processing is asynchronous — the endpoint returns immediately with a `store_id`.

```
POST /agents/{agent_id}/remember
```

**Request Body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Free-text memory content |
| `domain_tags` | string[] | no | Topic tags to apply to all resulting atoms |

**Response** `201`

```json
{
  "status": "queued",
  "store_id": "uuid"
}
```

---

### Recall

Retrieve memories by semantic search with optional graph expansion.

```
POST /agents/{agent_id}/recall
```

**Request Body**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | *required* | Natural-language search query |
| `domain_tags` | string[] | `null` | Filter to atoms matching these tags |
| `min_confidence` | float | `0.1` | Minimum effective confidence |
| `min_similarity` | float | `0.2` | Minimum cosine similarity |
| `max_results` | int | `10` | Maximum atoms to return |
| `expand_graph` | bool | `true` | Follow knowledge graph edges to find related atoms |
| `expansion_depth` | int | `2` | Maximum edge hops for graph expansion |
| `include_superseded` | bool | `false` | Include atoms that have been superseded by newer knowledge |
| `similarity_drop_threshold` | float | `0.3` | Stop returning results when relevance drops by this fraction between consecutive results |
| `verbosity` | string | `"full"` | `"full"`, `"summary"` (first sentence), or `"truncated"` |
| `max_content_chars` | int | `200` | Character limit when `verbosity="truncated"` |
| `max_total_tokens` | int | `null` | Approximate token budget for all results |

**Response** `200`

```json
{
  "atoms": [
    {
      "id": "uuid",
      "agent_id": "uuid",
      "atom_type": "procedural",
      "text_content": "Always specify dtype explicitly when using read_csv",
      "structured": {},
      "confidence_expected": 0.667,
      "confidence_effective": 0.61,
      "relevance_score": 0.87,
      "source_type": "direct_experience",
      "domain_tags": ["python", "pandas"],
      "created_at": "2026-03-15T10:00:00Z",
      "last_accessed": "2026-03-15T12:00:00Z",
      "access_count": 5,
      "is_active": true
    }
  ],
  "expanded_atoms": [],
  "total_retrieved": 1
}
```

---

## Atoms (Power-User)

Direct atom manipulation. Most users should use `/remember` and `/recall` instead.

### Create Atom

```
POST /agents/{agent_id}/atoms
```

**Request Body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `atom_type` | string | yes | `"episodic"`, `"semantic"`, `"procedural"`, or `"relational"` |
| `text_content` | string | yes | Memory text |
| `structured` | object | no | Arbitrary structured data (e.g. code snippets) |
| `confidence` | string | no | `"high"`, `"medium"`, `"low"`, or `"uncertain"` |
| `source_type` | string | no | Default: `"direct_experience"`. Also: `"inference"`, `"shared_view"`, `"imported_skill"`, `"consolidation"`, `"arc"` |
| `domain_tags` | string[] | no | Topic tags |

**Response** `201` — Atom object (same shape as recall results).

---

### Get Atom

```
GET /agents/{agent_id}/atoms/{atom_id}
```

**Response** `200` — Single atom object.

---

### Delete Atom

Soft-deletes the atom (marks as inactive).

```
DELETE /agents/{agent_id}/atoms/{atom_id}
```

**Response** `204` — No content.

---

### Create Edge

Link two atoms with a typed, weighted edge.

```
POST /agents/{agent_id}/atoms/link
```

**Request Body**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source_id` | uuid | *required* | Source atom ID |
| `target_id` | uuid | *required* | Target atom ID |
| `edge_type` | string | *required* | One of: `supports`, `contradicts`, `depends_on`, `generalises`, `specialises`, `motivated_by`, `evidence_for`, `supersedes`, `summarises`, `related` |
| `weight` | float | `1.0` | Edge weight (0.0 to 1.0) |

**Response** `201`

```json
{
  "id": "uuid",
  "source_id": "uuid",
  "target_id": "uuid",
  "edge_type": "supports",
  "weight": 1.0
}
```

---

## Views

### Create View

Create a snapshot of matching atoms. The set of atom IDs is frozen at creation time.

```
POST /agents/{agent_id}/views
```

**Request Body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | View name |
| `description` | string | no | Human-readable description |
| `atom_filter` | object | yes | Filter criteria (see below) |

**atom_filter fields:**

| Field | Type | Description |
|-------|------|-------------|
| `atom_types` | string[] | Filter by atom type |
| `domain_tags` | string[] | Filter by domain tags |
| `query` | string | Semantic search query to select atoms |
| `max_atoms` | int | Maximum atoms to include |

**Response** `201`

```json
{
  "id": "uuid",
  "owner_agent_id": "uuid",
  "name": "pandas-csv-handling",
  "description": "Procedural knowledge about CSV handling",
  "alpha": 1.0,
  "atom_filter": {"atom_types": ["procedural"], "domain_tags": ["pandas"]},
  "atom_count": 12,
  "created_at": "2026-03-15T10:00:00Z"
}
```

---

### List Views

```
GET /agents/{agent_id}/views
```

**Response** `200` — Array of view objects.

---

### Export Skill

Package a view's procedural atoms and their supporting semantic atoms into a structured skill document with rendered markdown.

```
GET /agents/{agent_id}/views/{view_id}/export_skill
```

**Response** `200`

```json
{
  "view_id": "uuid",
  "name": "pandas-csv-handling",
  "description": "Procedural knowledge about CSV handling",
  "domain_tags": ["python", "pandas"],
  "procedures": [ /* atom objects */ ],
  "supporting_facts": [ /* atom objects */ ],
  "metadata": {},
  "rendered_markdown": "# pandas-csv-handling\n\n## Procedures\n..."
}
```

---

### List Shared Views

List views that other agents have shared with this agent.

```
GET /agents/{agent_id}/shared_views
```

**Response** `200`

```json
[
  {
    "id": "uuid",
    "owner_agent_id": "uuid",
    "name": "pandas-csv-handling",
    "description": "Shared with you: CSV handling tips",
    "alpha": 1.0,
    "atom_filter": {},
    "atom_count": 12,
    "created_at": "2026-03-15T10:00:00Z",
    "grantor_id": "uuid",
    "source_address": "other-agent:operator.mnemo",
    "granted_at": "2026-03-15T11:00:00Z"
  }
]
```

---

### Recall via Shared View

Search within a specific shared view. Graph expansion is scope-bounded to the view's snapshot.

```
POST /agents/{agent_id}/shared_views/{view_id}/recall
```

**Request Body** — Same as [Recall](#recall).

**Response** `200` — Same shape as Recall response.

---

### Recall Across All Shared Views

Search across all views shared with this agent.

```
POST /agents/{agent_id}/shared_views/recall
```

**Request Body**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | *required* | Search query |
| `from_agent` | string | `null` | Filter to views shared by this agent address |
| `min_similarity` | float | `0.15` | Minimum cosine similarity |
| `max_results` | int | `5` | Maximum results |
| `verbosity` | string | `"summary"` | `"full"` or `"summary"` |
| `max_total_tokens` | int | `null` | Token budget |

**Response** `200`

```json
{
  "atoms": [
    {
      "id": "uuid",
      "atom_type": "procedural",
      "text_content": "Always specify dtype explicitly...",
      "confidence_effective": 0.61,
      "relevance_score": 0.87,
      "source_address": "other-agent:operator.mnemo"
    }
  ]
}
```

---

## Capabilities

### Grant Access

Grant another agent read access to a view.

```
POST /agents/{agent_id}/grant
```

**Request Body**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `view_id` | uuid | *required* | View to share |
| `grantee_id` | uuid | *required* | Agent receiving access |
| `permissions` | string[] | `["read"]` | Permission list |
| `expires_at` | datetime | `null` | Optional expiration |

**Response** `201`

```json
{
  "id": "uuid",
  "view_id": "uuid",
  "grantor_id": "uuid",
  "grantee_id": "uuid",
  "permissions": ["read"],
  "revoked": false,
  "expires_at": null,
  "created_at": "2026-03-15T10:00:00Z"
}
```

Granting is idempotent — if an active capability already exists for the same view and grantee, the existing capability is returned.

---

### Revoke Capability

```
POST /agents/{agent_id}/capabilities/{capability_id}/revoke
```

**Response** `200`

```json
{
  "capability_id": "uuid",
  "view_id": "uuid",
  "grantee_id": "uuid",
  "revoked": true,
  "revoked_at": "2026-03-15T10:00:00Z",
  "was_already_revoked": false
}
```

Revocation is idempotent. If already revoked, `was_already_revoked` is `true`.

---

### List Outbound Capabilities

List capabilities this agent has granted to others.

```
GET /agents/{agent_id}/capabilities?direction=outbound
```

**Response** `200`

```json
[
  {
    "capability_id": "uuid",
    "view_id": "uuid",
    "view_name": "pandas-csv-handling",
    "grantee_id": "uuid",
    "grantee_address": "other-agent:operator.mnemo",
    "permissions": ["read"],
    "revoked": false,
    "revoked_at": null,
    "granted_at": "2026-03-15T10:00:00Z"
  }
]
```

---

## Health

```
GET /health
```

**Response** `200`

```json
{
  "status": "ok"
}
```

---

## Error Responses

All errors return a JSON object with a `detail` field:

```json
{"detail": "Agent not found"}
```

| Status Code | Meaning |
|-------------|---------|
| `400` | Invalid request (bad UUID, missing field, etc.) |
| `401` | Missing or invalid API key |
| `403` | Permission denied (agent not owned by this operator) |
| `404` | Resource not found |
| `410` | Agent has departed |
| `500` | Server error |

---

# Python Client Library

Package: `mnemo-ai`

```bash
pip install mnemo-ai         # client only
pip install mnemo-ai[mcp]    # client + MCP server
```

## Async Client

```python
from mnemo.client import MnemoClient

async with MnemoClient(base_url="http://localhost:8000", api_key="mnemo_...") as client:
    ...
```

### Methods

#### Memory

| Method | Returns | Description |
|--------|---------|-------------|
| `remember(agent_id, text, domain_tags=None)` | `RememberResult` | Store a memory |
| `recall(agent_id, query, ...)` | `RecallResult` | Semantic search |
| `recall_shared(agent_id, view_id, query, ...)` | `dict` | Search within a shared view |
| `recall_all_shared(agent_id, query, ...)` | `dict` | Search across all shared views |

#### Agents

| Method | Returns | Description |
|--------|---------|-------------|
| `register_agent(name, persona=None, domain_tags=None, metadata=None)` | `dict` | Register a new agent |
| `get_agent(agent_id)` | `dict` | Get agent details |
| `find_agent_by_name(name)` | `list[dict]` | Search agents by name |
| `stats(agent_id)` | `AgentStats` | Get agent statistics |
| `depart(agent_id)` | `dict` | Depart agent (cascade revoke) |
| `resolve_address(address)` | `str` | Resolve address to UUID |
| `me()` | `dict` | Get authenticated operator info |
| `health()` | `dict` | Server health check |

#### Atoms (Power-User)

| Method | Returns | Description |
|--------|---------|-------------|
| `store_atom(agent_id, atom_type, text_content, ...)` | `dict` | Create a typed atom directly |
| `get_atom(agent_id, atom_id)` | `dict` | Get a single atom |
| `delete_atom(agent_id, atom_id)` | `None` | Soft-delete an atom |
| `link(agent_id, source_id, target_id, edge_type, weight=1.0)` | `dict` | Create an edge between atoms |

#### Views & Sharing

| Method | Returns | Description |
|--------|---------|-------------|
| `create_view(agent_id, name, atom_filter, description=None)` | `dict` | Create a snapshot view |
| `list_views(agent_id)` | `list[dict]` | List agent's views |
| `export_skill(agent_id, view_id)` | `dict` | Export view as skill document |
| `grant(agent_id, view_id, grantee_id, permissions=None, expires_at=None)` | `dict` | Grant view access |
| `revoke(capability_id)` | `dict` | Revoke a capability |
| `revoke_shared_view(agent_id, capability_id)` | `dict` | Revoke with agent scoping |
| `list_outbound_capabilities(agent_id)` | `list[dict]` | List capabilities you've granted |
| `list_shared_views(agent_id)` | `list[dict]` | List views shared with you |

### Recall Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | str | *required* | Search query |
| `domain_tags` | list[str] | `None` | Filter by domain |
| `min_confidence` | float | `0.1` | Minimum effective confidence |
| `min_similarity` | float | `0.2` | Minimum cosine similarity |
| `max_results` | int | `10` | Maximum results |
| `expand_graph` | bool | `True` | Enable graph expansion |
| `expansion_depth` | int | `2` | Max edge hops |
| `include_superseded` | bool | `False` | Include superseded atoms |
| `similarity_drop_threshold` | float | `None` | Gap-based cutoff |
| `verbosity` | str | `None` | `"full"`, `"summary"`, `"truncated"` |
| `max_total_tokens` | int | `None` | Token budget |

### Exceptions

| Exception | Trigger |
|-----------|---------|
| `MnemoError` | Base class for all errors |
| `MnemoAuthError` | 401 or 403 response |
| `MnemoNotFoundError` | 404 response |
| `MnemoServerError` | 5xx response |

## Sync Client

For non-async contexts. Wraps the async client with automatic event loop handling.

```python
from mnemo.client import MnemoClientSync

client = MnemoClientSync(api_key="mnemo_...", agent_id="uuid", base_url="http://localhost:8000")
client.remember("pandas.read_csv silently coerces mixed-type columns")
results = client.recall("loading CSV files")
stats = client.stats()
```

| Method | Returns | Description |
|--------|---------|-------------|
| `remember(text, domain_tags=None)` | `RememberResult` | Store a memory |
| `recall(query, **kwargs)` | `RecallResult` | Semantic search |
| `stats()` | `AgentStats` | Get agent stats |

The sync client is constructed with a fixed `agent_id`, so you don't pass it on each call.

---

# MCP Server

The MCP server wraps the Python client for use by AI assistants that support the Model Context Protocol (e.g. Claude Desktop).

## Configuration

| Environment Variable | Required | Default | Description |
|---------------------|----------|---------|-------------|
| `MNEMO_BASE_URL` | yes | — | Mnemo server URL |
| `MNEMO_API_KEY` | yes | — | API key for authentication |
| `MNEMO_DEFAULT_AGENT_ID` | no | — | Default agent UUID (avoids passing `agent_id` on every call) |
| `MNEMO_MCP_TRANSPORT` | no | `stdio` | Transport: `stdio`, `sse`, or `streamable-http` |
| `MNEMO_MCP_HOST` | no | `0.0.0.0` | Host for SSE/HTTP transports |
| `MNEMO_MCP_PORT` | no | `8001` | Port for SSE/HTTP transports |

## Running

```bash
# As a console script
mnemo-mcp

# As a Python module
python -m mnemo.mcp
```

## Claude Desktop Configuration

```json
{
  "mcpServers": {
    "mnemo": {
      "command": "mnemo-mcp",
      "env": {
        "MNEMO_BASE_URL": "http://localhost:8000",
        "MNEMO_API_KEY": "mnemo_...",
        "MNEMO_DEFAULT_AGENT_ID": "your-agent-uuid"
      }
    }
  }
}
```

## Tools

All tools accept an optional `agent_id` parameter. If `MNEMO_DEFAULT_AGENT_ID` is configured, it is used as the fallback.

---

### mnemo_remember

Store a memory. The server handles classification, confidence, and linking.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `text` | string | yes | What to remember. Be specific — include context, outcomes, and lessons. |
| `agent_id` | string | no | Agent UUID |
| `domain_tags` | string[] | no | Topic tags |

**Returns:** Confirmation with `store_id`.

---

### mnemo_recall

Search an agent's memories by meaning.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | *required* | What to search for |
| `agent_id` | string | — | Agent UUID |
| `domain_tags` | string[] | `null` | Filter by domain |
| `max_results` | int | `5` | Maximum results |
| `min_similarity` | float | `0.15` | Minimum similarity |
| `similarity_drop_threshold` | float | `0.3` | Gap-based cutoff |
| `verbosity` | string | `"summary"` | `"summary"` or `"full"` |
| `max_total_tokens` | int | `500` | Token budget |

**Returns:** Formatted results with confidence labels (high/moderate/low) and optional related atoms.

---

### mnemo_stats

View memory statistics for an agent.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_id` | string | no | Agent UUID |

**Returns:** Atom counts by type, edge count, average confidence, view and capability counts.

---

### mnemo_share

Share memories with another agent. Creates a view from a search query and grants read access.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | yes | What knowledge to share (used to select memories) |
| `share_with` | string | yes | Target agent address (e.g. `"agent-name:operator.org"`) |
| `name` | string | no | Name for the shared view |
| `domain_tags` | string[] | no | Filter by domain |
| `agent_id` | string | no | Agent UUID |

**Returns:** View ID, atom count, and capability ID.

---

### mnemo_list_shared

List shared memory views.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `direction` | string | `"inbound"` | `"inbound"` (shared with me) or `"outbound"` (shared by me) |
| `agent_id` | string | — | Agent UUID |

**Returns:** List of shared views with source/target info. Outbound results include capability IDs for revocation.

---

### mnemo_recall_shared

Search memories shared with this agent by other agents.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | *required* | What to search for |
| `from_agent` | string | `null` | Only search views from this agent address |
| `max_results` | int | `5` | Maximum results |
| `min_similarity` | float | `0.15` | Minimum similarity |
| `verbosity` | string | `"summary"` | `"summary"` or `"full"` |
| `max_total_tokens` | int | `500` | Token budget |
| `agent_id` | string | — | Agent UUID |

**Returns:** Results with source attribution (which agent shared each memory).

---

### mnemo_revoke_share

Revoke a previously shared view.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `capability_id` | string | yes | Capability ID (from `mnemo_list_shared` or `mnemo_share` response) |
| `agent_id` | string | no | Agent UUID |

**Returns:** Confirmation with view ID, recipient, and revocation timestamp.
