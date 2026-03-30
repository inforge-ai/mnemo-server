# Mnemo Server Technical Reference

## Overview

Mnemo is a persistent, permissioned memory server for AI agents. Agents store free text via `/remember`; the server decomposes it into typed atoms (episodic, semantic, procedural), embeds them, detects duplicates, and links them into a knowledge graph. Retrieval via `/recall` ranks atoms by a composite of embedding similarity and Bayesian confidence with temporal decay. Agents can share scoped snapshots of their memory with other agents through capability-controlled views.

## Codebase Organization

```
mnemo/
  server/
    main.py                 FastAPI app, lifespan, consolidation scheduling
    config.py               pydantic-settings (MNEMO_ env prefix)
    database.py             asyncpg pool (2-10 connections, 30s timeout)
    models.py               Pydantic request/response models
    embeddings.py           EmbeddingGemma-300M, thread-pool encode
    decomposer.py           Rule-based regex decomposer (fallback)
    llm_decomposer.py       Claude Haiku 4.5 structured extraction
    auth.py                 Three-tier auth: admin / operator / agent
    routes/
      memory.py             POST /remember, POST /recall, GET /stores/{id}/status
      agents.py             Agent CRUD, key rotation, stats
      views.py              View creation, shared recall, skill export
      shares.py             Operator share management (block/unblock)
      capabilities.py       Capability grant/revoke
      health.py             GET /health (public), GET /health/detailed (admin)
      auth.py               POST /auth/new-key, GET /auth/me
      atoms.py              Direct atom CRUD, edge creation
      admin.py              Admin middleware, operations, dashboard
      admin_operators.py    Operator CRUD, suspend/reinstate, key rotation
      admin_agents.py       Agent list/depart/reinstate/purge
      admin_trust.py        Global sharing toggle, share audit
    services/
      atom_service.py       Store and retrieve logic (core business layer)
      graph_service.py      Recursive CTE graph expansion
      view_service.py       Snapshot creation, shared recall
      consolidation.py      Background decay/cluster/merge pipeline
      agent_service.py      Departure, reinstatement
      auth_service.py       Key hashing (SHA-256), operator/key creation
      address_service.py    Agent address resolution
      migration_service.py  Schema migration auto-detection
  client/
    __init__.py             MnemoClient (httpx async client)
  mcp/
    server.py               MCP tool wrapper (FastMCP, stdio/SSE)
  cli.py                    Click CLI (operator + admin commands)
```

## Authentication

Three tiers, resolved by header priority:

| Tier | Header | Scope |
|------|--------|-------|
| Admin | `X-Admin-Key` | Platform management: operator CRUD, agent management, trust toggle |
| Agent | `X-Agent-Key` | Memory operations: remember, recall, share, stats |
| Operator | `X-Operator-Key` | Agent registration, key rotation, share inspection |

Keys are SHA-256 hashed at rest. Agent keys are stored on the `agents` table; operator keys in the `api_keys` table. Admin key is compared directly from `MNEMO_ADMIN_KEY` configuration.

If no recognized header is present, the request receives `401 Unauthorized`.

## Database Schema

### Core Tables

**operators** -- Billing and credential entity.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | gen_random_uuid() |
| name | TEXT UNIQUE | Display name |
| username | TEXT | Lowercase identifier |
| org | TEXT | Organization, default 'mnemo' |
| email | TEXT | |
| status | VARCHAR(16) | active / suspended / cancelled |
| sharing_scope | VARCHAR(5) | none / intra / full |
| stripe_customer_id | VARCHAR(64) | Optional billing |
| stripe_subscription_id | VARCHAR(64) | Optional billing |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

**agents** -- Scoped under operators.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | uuid_generate_v4() |
| operator_id | UUID FK operators | CASCADE delete |
| name | TEXT | UNIQUE per operator |
| persona | TEXT | Optional agent description |
| domain_tags | TEXT[] | GIN-indexed |
| metadata | JSONB | Arbitrary metadata |
| status | VARCHAR(16) | active / departed |
| departed_at | TIMESTAMPTZ | NULL when active |
| data_expires_at | TIMESTAMPTZ | departed_at + 30 days |
| key_hash | TEXT | SHA-256 of agent key |
| key_prefix | VARCHAR(20) | First 16 chars for display |
| created_at | TIMESTAMPTZ | |
| last_active_at | TIMESTAMPTZ | |

**agent_addresses** -- Canonical format: `agent_name:operator_username.org`

| Column | Type | Notes |
|--------|------|-------|
| agent_id | UUID PK | FK agents, CASCADE |
| address | TEXT UNIQUE | Canonical address |
| created_at | TIMESTAMPTZ | |

**atoms** -- Core memory units.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | uuid_generate_v4() |
| agent_id | UUID FK agents | CASCADE delete |
| atom_type | TEXT | episodic / semantic / procedural / relational |
| text_content | TEXT | |
| structured | JSONB | Default '{}' |
| embedding | vector(768) | EmbeddingGemma-300M, ivfflat indexed |
| confidence_alpha | FLOAT | Beta distribution, default 2.0 |
| confidence_beta | FLOAT | Beta distribution, default 2.0 |
| source_type | TEXT | direct_experience / inference / shared_view / imported_skill / consolidation / arc |
| source_ref | UUID | Reference to source atom |
| domain_tags | TEXT[] | GIN-indexed |
| decay_type | TEXT | exponential / linear / none |
| decay_half_life_days | FLOAT | Default 30.0; type-specific |
| is_active | BOOLEAN | Soft delete, default true |
| access_count | INTEGER | Incremented on retrieval |
| last_accessed | TIMESTAMPTZ | Updated on retrieval |
| created_at | TIMESTAMPTZ | |
| decomposer_version | TEXT | 'haiku_v1' or 'regex_v1' |
| last_consolidated_at | TIMESTAMPTZ | Consolidation tracking |

**edges** -- Knowledge graph relations.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| source_id | UUID FK atoms | CASCADE |
| target_id | UUID FK atoms | CASCADE |
| edge_type | TEXT | related / causal / temporal_sequence / supports / contradicts / depends_on / generalises / specialises / motivated_by / evidence_for / supersedes / summarises |
| weight | FLOAT | 0.0-1.0, default 1.0 |
| created_at | TIMESTAMPTZ | |

Constraint: UNIQUE (source_id, target_id, edge_type).

**views** -- Frozen atom snapshots.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| owner_agent_id | UUID FK agents | CASCADE |
| name | TEXT | |
| description | TEXT | Optional |
| alpha | FLOAT | Default 1.0 |
| atom_filter | JSONB | {atom_types, domain_tags, query} |
| snapshot_at | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | |

**snapshot_atoms** -- Immutable scope boundary.

| Column | Type | Notes |
|--------|------|-------|
| view_id | UUID FK views | CASCADE |
| atom_id | UUID FK atoms | CASCADE |

PK: (view_id, atom_id).

**capabilities** -- Access grants forming a delegation tree.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| view_id | UUID FK views | CASCADE |
| grantor_id | UUID FK agents | |
| grantee_id | UUID FK agents | |
| permissions | TEXT[] | Default '{read}' |
| revoked | BOOLEAN | Default false |
| revoked_at | TIMESTAMPTZ | |
| parent_cap_id | UUID FK capabilities | Delegation chain |
| expires_at | TIMESTAMPTZ | Optional TTL |
| blocked_by_recipient | BOOLEAN | Operator block |
| created_at | TIMESTAMPTZ | |

**agent_trust** -- Bidirectional trust for sharing.

| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| agent_uuid | UUID FK agents | CASCADE |
| trusted_sender_uuid | UUID FK agents | CASCADE |
| note | TEXT | Optional |
| created_at | TIMESTAMPTZ | |

Constraint: UNIQUE (agent_uuid, trusted_sender_uuid).

### Auxiliary Tables

| Table | Purpose |
|-------|---------|
| api_keys | Operator API keys (hashed), with is_active flag |
| access_log | Immutable audit trail (agent_id, action, target_id, metadata) |
| operations | Per-call record (operation type, duration_ms, metadata) |
| store_jobs | Async remember job tracking (status, atoms_created, error) |
| store_failures | Failed store jobs with original text and error |
| decomposer_usage | LLM token usage per operator (input, output, cache stats) |
| platform_config | Key-value config (e.g., sharing_enabled) |
| schema_migrations | Applied migration versions |

### Database Functions

**effective_confidence(alpha, beta, decay_type, half_life, created_at, last_accessed, access_count) -> FLOAT**

Computes confidence after temporal decay:

```
base = alpha / (alpha + beta)
if decay_type = 'none': return base
age = days since last_accessed (or created_at)
access_bonus = ln(1 + access_count) * 0.1
if exponential: factor = 0.5 ^ (age / (half_life * (1 + access_bonus)))
if linear:      factor = max(0, 1 - age / (2 * half_life * (1 + access_bonus)))
return base * factor
```

**revoke_agent_capabilities(departing_agent_id) -> INTEGER**

Cascade-revokes all capabilities granted by the departing agent and their transitive children via recursive CTE on `parent_cap_id`.

---

## API Reference

All endpoints are prefixed with `/v1/`.

### Memory

#### POST /agents/{agent_id}/remember

Store a memory. Returns immediately; decomposition runs asynchronously.

**Auth**: Agent key required.

**Request**:
```json
{
  "text": "string (1-50,000 chars, required)",
  "domain_tags": ["string"],
  "remembered_on": "2024-01-15T00:00:00Z"
}
```

**Response** (201):
```json
{
  "status": "queued",
  "store_id": "uuid"
}
```

The `remembered_on` field provides temporal context for the decomposer to resolve relative time references ("last Saturday" -> absolute date).

#### POST /agents/{agent_id}/recall

Retrieve memories by semantic query.

**Auth**: Agent key required.

**Request**:
```json
{
  "query": "string (1-2,000 chars, required)",
  "domain_tags": ["string"],
  "min_confidence": 0.1,
  "min_similarity": 0.25,
  "max_results": 10,
  "expand_graph": true,
  "expansion_depth": 2,
  "include_superseded": false,
  "similarity_drop_threshold": 0.15,
  "verbosity": "full",
  "max_content_chars": 200,
  "max_total_tokens": null
}
```

All fields except `query` are optional with the defaults shown.

**Response** (200):
```json
{
  "atoms": [
    {
      "id": "uuid",
      "agent_id": "uuid",
      "atom_type": "episodic",
      "text_content": "string",
      "structured": {},
      "confidence_expected": 0.889,
      "confidence_effective": 0.842,
      "relevance_score": 0.731,
      "source_type": "direct_experience",
      "domain_tags": ["string"],
      "created_at": "datetime",
      "last_accessed": "datetime",
      "access_count": 5,
      "is_active": true,
      "confidence_alpha": 8.0,
      "confidence_beta": 1.0
    }
  ],
  "expanded_atoms": [],
  "total_retrieved": 1
}
```

`confidence_alpha` and `confidence_beta` are only included when `verbosity="full"`. Verbosity `"summary"` truncates text at first sentence; `"truncated"` cuts to `max_content_chars`.

#### GET /stores/{store_id}/status

Check async remember job status.

**Auth**: Operator or admin.

**Response** (200):
```json
{
  "store_id": "uuid",
  "status": "complete",
  "atoms_created": 12,
  "created_at": "datetime",
  "completed_at": "datetime",
  "error": null
}
```

Status values: `pending`, `decomposing`, `complete`, `failed`.

---

### Agents

#### POST /agents

Register a new agent.

**Auth**: Operator key required.

**Request**:
```json
{
  "name": "string (1-255 chars, required)",
  "persona": "string (optional, max 2,000 chars)",
  "domain_tags": ["string"],
  "metadata": {}
}
```

**Response** (201):
```json
{
  "id": "uuid",
  "name": "string",
  "persona": "string",
  "domain_tags": [],
  "metadata": {},
  "created_at": "datetime",
  "status": "active",
  "address": "agent-name:operator.org",
  "agent_key": "mnemo_ag_..."
}
```

The `agent_key` is shown once. Save it -- it cannot be retrieved again. Agent names must be unique per operator. Returns `409` if name already exists.

#### GET /agents

List operator's agents.

**Auth**: Operator key required. Operators see only their own agents; admin sees all.

**Query params**: `name` (optional exact match filter).

**Response** (200): Array of agent objects (without `agent_key`).

#### GET /agents/{agent_id}

Get a single agent by UUID or address.

**Auth**: Operator key required. Must own the agent (admin bypasses).

**Response** (200): Agent object. Returns `404` if not found or not owned.

#### GET /agents/resolve/{address}

Resolve an agent address to agent info.

**Auth**: Any authenticated user.

**Response** (200):
```json
{
  "agent_id": "uuid",
  "name": "string",
  "address": "string",
  "operator": "string"
}
```

#### GET /agents/{agent_id}/stats

Agent memory statistics.

**Auth**: Agent key required (must match).

**Response** (200):
```json
{
  "agent_id": "uuid",
  "total_atoms": 150,
  "active_atoms": 142,
  "atoms_by_type": {"episodic": 80, "semantic": 50, "procedural": 12},
  "arc_atoms": 8,
  "total_edges": 340,
  "avg_effective_confidence": 0.72,
  "active_views": 2,
  "granted_capabilities": 1,
  "received_capabilities": 3,
  "address": "agent-name:operator.org",
  "topics": [],
  "date_range": {},
  "most_accessed": [{"text": "...", "hits": 15}]
}
```

#### POST /agents/{agent_id}/rotate-key

Rotate an agent's API key.

**Auth**: Operator key (must own agent) or admin.

**Response** (200):
```json
{
  "agent_id": "uuid",
  "name": "string",
  "address": "string",
  "agent_key": "mnemo_ag_...",
  "message": "Save this key — it will not be shown again. The previous key is now invalid."
}
```

#### POST /agents/{agent_id}/depart

Depart an agent. Cascade-revokes all capabilities.

**Auth**: Admin required.

**Response** (200): Departure summary with `capabilities_revoked` count and `data_expires_at`.

Returns `409` if already departed.

#### POST /agents/{agent_id}/reactivate

Reinstate a departed agent. Revoked capabilities are NOT restored.

**Auth**: Admin required.

**Response** (200): Reactivation summary. Returns `409` if not departed.

---

### Atoms (Power-User Interface)

#### POST /agents/{agent_id}/atoms

Create a typed atom directly (bypasses decomposer).

**Auth**: Agent key required.

**Request**:
```json
{
  "atom_type": "episodic",
  "text_content": "string (1-10,000 chars)",
  "structured": {},
  "confidence": "high",
  "source_type": "direct_experience",
  "source_ref": null,
  "domain_tags": []
}
```

Confidence values: `high` -> Beta(8,1), `medium` -> Beta(4,2), `low` -> Beta(2,3), `uncertain` -> Beta(2,4).

**Response** (201): AtomResponse.

#### GET /agents/{agent_id}/atoms/{atom_id}

Fetch a single atom.

**Auth**: Agent key required.

**Response** (200): AtomResponse. Returns `404` if not found.

#### DELETE /agents/{agent_id}/atoms/{atom_id}

Soft-delete an atom (sets `is_active=false`).

**Auth**: Agent key required.

**Response**: 204 No Content.

#### POST /agents/{agent_id}/atoms/link

Create an edge between two atoms.

**Auth**: Agent key required.

**Request**:
```json
{
  "source_id": "uuid",
  "target_id": "uuid",
  "edge_type": "supports",
  "weight": 1.0
}
```

Edge types: `supports`, `contradicts`, `depends_on`, `generalises`, `specialises`, `motivated_by`, `evidence_for`, `supersedes`, `summarises`, `related`.

**Response** (201): EdgeResponse. Returns `409` if edge already exists.

---

### Views & Sharing

#### POST /agents/{agent_id}/views

Create a snapshot view.

**Auth**: Agent key required.

**Request**:
```json
{
  "name": "string (1-255 chars)",
  "description": "string (optional)",
  "atom_filter": {
    "atom_types": ["procedural"],
    "domain_tags": ["python"],
    "query": "debugging techniques"
  }
}
```

The `atom_filter` is evaluated at creation time. Matching atom IDs are frozen into `snapshot_atoms`.

**Response** (201):
```json
{
  "id": "uuid",
  "owner_agent_id": "uuid",
  "name": "string",
  "description": "string",
  "alpha": 1.0,
  "atom_filter": {},
  "atom_count": 42,
  "created_at": "datetime"
}
```

#### GET /agents/{agent_id}/views

List views owned by agent.

**Auth**: Agent key required.

**Response** (200): Array of ViewResponse.

#### POST /agents/{agent_id}/grant

Grant view access to another agent.

**Auth**: Agent key required (must own view).

**Request**:
```json
{
  "view_id": "uuid",
  "grantee_id": "uuid",
  "permissions": ["read"],
  "expires_at": null
}
```

**Response** (201): CapabilityResponse.

Validates: global sharing enabled, operator sharing scope permits grant, grantee exists and is active. Idempotent -- returns existing capability if already granted. Returns `403` if sharing disabled, `410` if grantee departed.

#### POST /capabilities/{cap_id}/revoke

Revoke a capability. Cascades through delegation tree.

**Auth**: Agent key required (must be grantor).

**Response** (200):
```json
{
  "revoked": true,
  "cascade_revoked": 3
}
```

#### GET /agents/{agent_id}/capabilities

List capabilities granted by agent (outbound).

**Auth**: Agent key required.

**Response** (200): Array of OutboundCapabilityResponse including `view_name`, `grantee_address`, `revoked`, `granted_at`.

#### POST /agents/{agent_id}/shared_views/{view_id}/recall

Recall through a shared view.

**Auth**: Agent key required (must be grantee).

**Request**: Same as `/recall`.

**Response** (200): Same as `/recall`. Graph expansion is bounded to `snapshot_atoms` only.

Validates: capability non-revoked/non-expired/non-blocked, bidirectional trust exists. Returns empty results silently if trust check fails (privacy-safe).

#### POST /agents/{agent_id}/shared_views/recall

Recall across all views shared with this agent.

**Auth**: Agent key required.

**Request**:
```json
{
  "query": "string (1-2,000 chars)",
  "from_agent": "address (optional, filter by grantor)",
  "min_similarity": 0.15,
  "max_results": 5,
  "verbosity": "summary",
  "max_total_tokens": 500
}
```

**Response** (200): Atoms include `source_address` and `view_name` for attribution.

#### GET /agents/{agent_id}/shared_views

List all views shared with this agent.

**Auth**: Agent key required.

**Response** (200): Array of SharedViewResponse including `grantor_id`, `source_address`, `granted_at`, `trusted`.

#### GET /agents/{agent_id}/views/{view_id}/export_skill

Export view as a SKILL.md-format document.

**Auth**: Agent key required (must own view).

**Response** (200):
```json
{
  "view_id": "uuid",
  "name": "string",
  "description": "string",
  "domain_tags": [],
  "procedures": [],
  "supporting_facts": [],
  "metadata": {},
  "rendered_markdown": "# Skill: ..."
}
```

---

### Operator Share Management

#### GET /operators/me/shares

List inbound and outbound shares for all operator's agents.

**Auth**: Operator key required.

**Response** (200):
```json
{
  "inbound": [
    {
      "capability_id": "uuid",
      "grantor_address": "agent:op.org",
      "grantee_address": "agent:op.org",
      "view_name": "string",
      "atom_count": 42,
      "blocked": false,
      "created_at": "datetime"
    }
  ],
  "outbound": []
}
```

#### POST /shares/{capability_id}/block

Block an inbound share (operator-level).

**Auth**: Operator key required. Grantee must belong to operator.

**Response** (200): `{"capability_id": "uuid", "blocked": true}`.

#### POST /shares/{capability_id}/unblock

Unblock a previously blocked share.

**Auth**: Operator key required.

**Response** (200): `{"capability_id": "uuid", "blocked": false}`.

---

### Auth

#### GET /auth/me

Identity check.

**Auth**: Operator key required.

**Response** (200):
```json
{
  "id": "uuid",
  "name": "string",
  "role": "operator",
  "agent_count": 3,
  "sharing_scope": "intra"
}
```

#### POST /auth/new-key

Generate an additional operator API key. Existing keys remain valid.

**Auth**: Operator key required.

**Response** (200):
```json
{
  "operator_id": "uuid",
  "api_key": "mnemo_...",
  "message": "Save this key — it will not be shown again."
}
```

---

### Health

#### GET /health

Public health check.

**Auth**: None.

**Response** (200):
```json
{
  "status": "ok",
  "version": "0.1.0+c2363c8",
  "schema_version": "003_sharing_scope",
  "uptime_seconds": 3600,
  "postgres": "ok"
}
```

#### GET /health/detailed

Detailed health with resource counts.

**Auth**: Admin required.

**Response** (200): Extends `/health` with `sharing_enabled`, `operator_count`, `agent_count`, `atom_count`, `embedding_model`, `embedding_dimensions`, `postgres_version`, `pgvector_version`, and `config` (min_similarity, decomposer type).

---

### Admin: Operators

All admin endpoints require `X-Admin-Key` header.

#### POST /admin/operators

Create operator with initial API key.

**Request**:
```json
{
  "username": "jdoe",
  "org": "acme",
  "display_name": "Jane Doe",
  "email": "jane@acme.com"
}
```

Username and org must match `^[a-z][a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$`.

**Response** (201): Operator object with one-time `api_key`.

#### GET /admin/operators

List all operators with agent counts.

**Response** (200): `{"operators": [...]}` including `sharing_scope` and `agent_count`.

#### GET /admin/operators/{operator_id}

Get operator with list of their agents.

**Response** (200): Operator object with `agents` array.

#### POST /admin/operators/{operator_id}/suspend

Suspend operator. Departs all active agents.

**Response** (200): `{"uuid", "username", "status": "suspended", "agents_departed": N}`.

#### POST /admin/operators/{operator_id}/reinstate

Reinstate suspended operator. Agents remain departed.

**Response** (200): Operator object with note about manual agent reinstatement.

#### POST /admin/operators/{operator_id}/rotate-key

Deactivate all existing keys and issue new one.

**Response** (200): Operator object with one-time `api_key`.

#### PATCH /admin/operators/{operator_id}/sharing-scope

Set operator sharing scope.

**Request**: `{"sharing_scope": "none|intra|full"}`.

**Response** (200): Updated operator object.

---

### Admin: Agents

#### GET /admin/agents

List all agents across operators.

**Query params**: `operator` (UUID filter), `status` (active/departed filter).

**Response** (200): `{"agents": [...]}` with `active_atoms`, `total_atoms`, `operator_username`.

#### POST /admin/agents/{agent_id}/depart

Admin force-depart (no ownership check).

**Response** (200): Departure summary.

#### POST /admin/agents/{agent_id}/reinstate

Admin reinstate (no ownership check).

**Response** (200): Reinstatement summary.

#### POST /admin/agents/{agent_id}/rotate-key

Admin rotate agent key.

**Response** (200): Agent object with one-time `agent_key`.

#### POST /admin/agents/{agent_id}/purge

Hard-delete all agent data. Requires confirmation.

**Request**: `{"confirm": "purge"}`.

**Response** (200):
```json
{
  "agent_id": "uuid",
  "atoms_deleted": 150,
  "edges_deleted": 340,
  "shares_revoked": 2,
  "status": "departed"
}
```

---

### Admin: Trust & Sharing

#### GET /admin/trust/status

Check global sharing toggle.

**Response** (200): `{"sharing_enabled": true}`.

#### POST /admin/trust/enable

Enable sharing globally.

**Response** (200): `{"sharing_enabled": true}`.

#### POST /admin/trust/disable

Disable sharing globally. Existing shares are suspended, not deleted.

**Response** (200): `{"sharing_enabled": false, "note": "..."}`.

#### GET /admin/trust/shares

List active shares platform-wide.

**Query params**: `operator` (UUID), `agent` (UUID, matches grantor or grantee).

**Response** (200): `{"shares": [...]}` with addresses, view names, atom counts.

#### DELETE /admin/trust/shares/{capability_id}

Admin revoke a share with cascade.

**Response** (200): `{"capability_id", "revoked": true, "cascade_count": N}`.

---

### Admin: Dashboard

#### GET /admin/glance

Quick dashboard stats.

**Response** (200): `{"items": [{"title": "Agents", "value": "4"}, ...]}` covering agents, atoms, today's operations.

#### GET /admin/operations

Operation counts by type.

**Query params**: `target_id` (agent UUID filter).

**Response** (200): `{"total": N, "by_operation": [{"operation": "recall", "total": N, "avg_duration_ms": N, "last_at": "datetime"}]}`.

#### GET /admin/keys

List all API keys with operator info.

**Response** (200): Array of key objects with `key_prefix`, `is_active`, `operator_name`, `last_used_at`.

---

## Configuration

All settings via `MNEMO_` environment prefix. Source: environment variables or `.env` file.

### Required

| Variable | Purpose |
|----------|---------|
| `MNEMO_DATABASE_URL` | PostgreSQL connection string |
| `MNEMO_ADMIN_KEY` | Admin authentication key |

### Optional

| Variable | Default | Purpose |
|----------|---------|---------|
| `MNEMO_DOCKER_DATABASE_URL` | | DB URL as seen from inside Docker container |
| `ANTHROPIC_API_KEY` | | Enables LLM decomposer (Haiku); without it, regex fallback |
| `HF_TOKEN` | | Required if embedding model is gated (EmbeddingGemma) |
| `MNEMO_EMBEDDING_MODEL` | `google/embeddinggemma-300m` | Embedding model name |
| `MNEMO_EMBEDDING_DIM` | `768` | Embedding dimensions |
| `MNEMO_DUPLICATE_SIMILARITY_THRESHOLD` | `0.90` | Cosine threshold for duplicate detection |
| `MNEMO_CROSS_CALL_EDGE_THRESHOLD` | `0.55` | Cosine threshold for cross-call edges |
| `MNEMO_CONSOLIDATION_INTERVAL_MINUTES` | `60` | Background consolidation frequency |
| `MNEMO_MIN_EFFECTIVE_CONFIDENCE` | `0.05` | Floor for decay reaping |
| `MNEMO_DECAY_EPISODIC` | `14.0` | Episodic half-life (days) |
| `MNEMO_DECAY_SEMANTIC` | `90.0` | Semantic half-life (days) |
| `MNEMO_DECAY_PROCEDURAL` | `180.0` | Procedural half-life (days) |
| `MNEMO_DECAY_RELATIONAL` | `90.0` | Relational half-life (days) |
| `MNEMO_DEPARTURE_RETENTION_DAYS` | `30` | Data retention after agent departure |
| `MNEMO_MAX_RETRIEVAL_RESULTS` | `50` | Hard cap on retrieval results |
| `MNEMO_DEFAULT_RETRIEVAL_LIMIT` | `10` | Default max_results |
| `MNEMO_GRAPH_EXPANSION_MAX_DEPTH` | `3` | Maximum graph expansion depth |
| `MNEMO_SYNC_STORE_FOR_TESTS` | `false` | If true, /remember awaits inline (for tests) |
| `MNEMO_TEST_DATABASE_URL` | | Test database connection |
