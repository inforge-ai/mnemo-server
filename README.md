# Mnemo
[![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)
[![LoCoMo](https://img.shields.io/badge/LoCoMo-81.6%25-brightgreen.svg)](https://github.com/snap-research/locomo)

**Mnemo** is a persistent, permissioned, portable memory server for AI agents. It stores typed memory atoms with semantic retrieval, knowledge graph relationships, decay dynamics, and view sharing/skill export.

## Status

All core phases are complete:

| Feature | Status |
|---|---|
| FastAPI server + DB pool | Done |
| Rule-based decomposer (episodic/semantic/procedural) | Done |
| Arc decomposer (structural summary atoms) | Done |
| Atom storage, embedding, duplicate detection | Done |
| Semantic recall with confidence decay | Done |
| Recall controls (gap threshold, verbosity, token budget) | Done |
| Knowledge graph edges + expansion | Done |
| Views and snapshot sharing | Done |
| Capabilities / permission grants | Done |
| Sharing trust auth (directional trust gating) | Done |
| Background consolidation (decay, clustering, merge, purge) | Done |
| Operator-scoped authentication (API keys, ownership) | Done |
| Admin API (agents, operations, keys, glance) | Done |
| CLI (operator/agent/trust management, admin subcommands) | Done |
| MCP server (Claude tool interface) | Done |
| Multi-agent MCP support | Done |

Not yet built (v0.1 scope exclusions): rate limiting, live view subscriptions, contradiction detection, horizontal scaling, skill files, any UI.

## Requirements

- Python 3.12
- PostgreSQL 16 with pgvector
- `uv` (package manager)

## Setup

### Database

```bash
sudo apt install postgresql-16 postgresql-16-pgvector
sudo -u postgres createdb mnemo
sudo -u postgres psql mnemo -c "CREATE EXTENSION IF NOT EXISTS vector;"
sudo -u postgres psql mnemo -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"
sudo -u postgres psql mnemo -f schema.sql
```

Connection: `postgresql://USER:PASSWORD@HOST:PORT/DB_NAME`. Override via `MNEMO_DATABASE_URL`.

### Install and run

```bash
uv run uvicorn mnemo.server.main:app --reload
```

## Authentication

Mnemo uses RBAC with three credential types, passed as request headers:

| Header | Role | Use |
|--------|------|-----|
| `X-Admin-Key` | admin | Full access (operator CRUD, trust management, all endpoints) |
| `X-Operator-Key` | operator | Management-plane (register agents, inspect shares, rotate keys) |
| `X-Agent-Key` | agent | Data-plane (remember, recall, stats, views, capabilities) |

### CLI quickstart

The `mnemo` CLI is installed as an entry point (see `pyproject.toml`). You can also invoke it via `uv run python -m mnemo.cli`.

**Step 1: Create an operator (admin-only)**

```bash
export MNEMO_ADMIN_TOKEN="..."

# Create operator — returns an operator API key (show-once)
mnemo admin operator create --username jdoe --org acme --display-name "Jane Doe" --email jane@acme.com
```

**Step 2: Operator commands** (require `MNEMO_API_KEY`)

```bash
export MNEMO_API_KEY="mnemo_..."   # operator key from step 1

# Check identity
mnemo whoami

# Create an agent — returns an agent key (show-once)
mnemo create-agent my-agent --persona "A Python developer" --tags python,backend

# List your agents
mnemo list-agents

# Rotate an agent's key (returns new key once, invalidates old)
mnemo rotate-agent-key <agent_id>

# Generate an additional operator API key
mnemo new-key
```

### CLI admin commands

Admin commands require `MNEMO_ADMIN_TOKEN` (via env var or `--admin-token`). All admin subcommands accept `--json` for raw JSON output.

```bash
export MNEMO_ADMIN_TOKEN="..."

# ── Operator management ──
mnemo admin operator create --username jdoe --org acme --display-name "Jane Doe" --email jane@acme.com
mnemo admin operator list
mnemo admin operator show <operator_id>
mnemo admin operator suspend <operator_id>
mnemo admin operator reinstate <operator_id>
mnemo admin operator rotate-key <operator_id>
mnemo admin operator set-sharing-scope <operator_id> none|intra|full

# ── Agent management ──
mnemo admin agent list [--operator <uuid>] [--status active|departed]
mnemo admin agent depart <agent_id>
mnemo admin agent reinstate <agent_id>
mnemo admin agent rotate-key <agent_id>

# ── Trust / sharing management ──
mnemo admin trust status           # show sharing enabled/disabled
mnemo admin trust enable           # enable sharing globally
mnemo admin trust disable          # disable sharing globally
mnemo admin trust list [--operator <uuid>] [--agent <uuid>]   # list active shares
mnemo admin trust revoke <capability_id>                      # revoke a capability (cascade)
```

### REST auth endpoints
- `POST /auth/new-key` — generate additional operator API key (`X-Operator-Key` required)
- `GET /auth/me` — return current operator/agent info

When auth is enabled, endpoints enforce role-based access. Operators can only manage their own agents; agents can only access their own data.

## Working with Agents

### Register an agent

Requires an operator key. The response includes a one-time `agent_key` — save it.

```bash
curl -s -X POST http://api.example.com/v1/agents \
  -H "Content-Type: application/json" \
  -H "X-Operator-Key: $MNEMO_API_KEY" \
  -d '{
    "name": "my-agent",
    "persona": "A Python backend developer",
    "domain_tags": ["python", "backend"],
    "metadata": {}
  }'
```

Save the `agent_key` from the response — it will not be shown again. Use it as `X-Agent-Key` for all data-plane calls.

### Store a memory

Submit free text. The server decomposes it into typed atoms (episodic, semantic, procedural), generates embeddings, deduplicates, and links related atoms automatically.

```bash
curl -s -X POST http://api.example.com/v1/agents/$AGENT_ID/remember \
  -H "Content-Type: application/json" \
  -H "X-Agent-Key: $MNEMO_AGENT_KEY" \
  -d '{
    "text": "I learned that asyncpg is faster than psycopg2 for async workloads. I switched our connection pool to asyncpg today. Always use asyncpg for FastAPI projects.",
    "domain_tags": ["python", "databases"]
  }'
```

### Recall memories

Retrieve relevant memories via semantic search, filtered by confidence and similarity, with optional knowledge graph expansion.

```bash
curl -s -X POST http://api.example.com/v1/agents/$AGENT_ID/recall \
  -H "Content-Type: application/json" \
  -H "X-Agent-Key: $MNEMO_AGENT_KEY" \
  -d '{
    "query": "which database library should I use with FastAPI?",
    "max_results": 5,
    "expand_graph": true,
    "min_similarity": 0.3,
    "verbosity": "summary"
  }'
```

### Get agent stats

```bash
curl -s http://api.example.com/v1/agents/$AGENT_ID/stats \
  -H "X-Agent-Key: $MNEMO_AGENT_KEY"
```

### Agent departure

Marks an agent as departed, cascade-revokes all capabilities it granted, and schedules its data for deletion after 30 days. Requires admin access.

```bash
curl -s -X POST http://api.example.com/v1/admin/agents/$AGENT_ID/depart \
  -H "X-Admin-Key: $MNEMO_ADMIN_TOKEN"
```

## Sharing

### Sharing scope

Each operator has a `sharing_scope` that controls what sharing operations are allowed:

| Scope | Behaviour | Tier |
|-------|-----------|------|
| `none` | No sharing. All share/recall_shared/list_shared operations return 403. | Free / Individual |
| `intra` | Sharing only between agents with the same operator. Cross-operator attempts return 403. | Team |
| `full` | Unrestricted sharing (existing capability model). | Enterprise |

New operators default to `none`. Set the scope via admin CLI:

```bash
mnemo admin operator set-sharing-scope <operator_id> intra
```

There is also a global sharing kill-switch (`admin trust enable/disable`) that overrides per-operator scope when disabled.

### Trust auth

Mnemo uses directional trust to gate memory sharing. When agent B holds a capability granted by agent A, B can only recall shared memories if a trust row exists (`agent_trust`) from B toward A. Without trust, `recall_shared` and `recall_all_shared` return empty results silently.

Trust and sharing are managed via the admin CLI (requires `MNEMO_ADMIN_TOKEN`, never exposed as MCP tools):

```bash
# Check if sharing is enabled globally
mnemo admin trust status

# Enable/disable sharing
mnemo admin trust enable
mnemo admin trust disable

# List active shares/capabilities (filterable by operator or agent)
mnemo admin trust list [--operator <uuid>] [--agent <uuid>]

# Revoke a capability with cascade
mnemo admin trust revoke <capability_id>
```

**Auto-seeding:** When an agent is created under an operator with an `org` field, symmetric trust rows are automatically created between the new agent and all existing agents in the same org.

**`list_shared_views` response** includes a `trusted` boolean per view, so MCP tools can display trust status without extra queries.

## MCP Server (Claude integration)

Mnemo ships an MCP server so Claude can use it as a memory tool directly. Agent identity is resolved by name (unique per operator).

```bash
export MNEMO_BASE_URL="http://api.example.com"
export MNEMO_API_KEY="mnemo_..."
export MNEMO_AGENT_NAME="my-agent"
export MNEMO_AGENT_PERSONA="A Python backend developer"
export MNEMO_DOMAIN_TAGS="python,backend"

python -m mnemo.mcp.mcp_server
```

Tools exposed: `mnemo_remember`, `mnemo_recall`, `mnemo_stats`. All support an optional `agent_id` parameter for multi-agent use.

## Admin API

Admin endpoints are protected by the `X-Admin-Token` header (or `?token=` query param), configured via `MNEMO_ADMIN_TOKEN`.

- `GET /v1/admin/agents` — list all agents across operators
- `GET /v1/admin/operations` — recent operations log
- `GET /v1/admin/keys` — list all API keys with operator info
- `GET /v1/admin/glance` — system overview dashboard

## Running tests

```bash
uv run pytest
```

## Architecture

```
mnemo/
├── server/
│   ├── main.py           # FastAPI app, lifespan, consolidation scheduling
│   ├── config.py         # pydantic-settings (MNEMO_ env prefix)
│   ├── database.py       # asyncpg connection pool
│   ├── models.py         # Pydantic request/response models
│   ├── embeddings.py     # sentence-transformers (all-MiniLM-L6-v2, 384-dim)
│   ├── decomposer.py     # rule-based free-text -> typed atoms + arc synthesis
│   └── routes/           # agents, memory, atoms, views, capabilities, auth, admin
│   └── services/         # atom_service, graph_service, view_service, consolidation
├── cli.py                  # CLI entry point (operator, agent, trust, admin)
├── client/mnemo_client.py  # async httpx client
├── mcp/mcp_server.py       # MCP wrapper for Claude
└── tests/
```

**Core data flow:**

1. Agent submits free text to `/remember`
2. Decomposer classifies sentences into typed atoms (episodic, semantic, procedural) with inferred confidence (Beta distribution)
3. Embeddings generated; duplicates (cosine > 0.90) are merged rather than stored
4. Atoms stored; edges created between co-submitted atoms; arc atom summarises the batch
5. On `/recall`: query embedded -> vector similarity search -> confidence decay applied -> knowledge graph optionally expanded -> `last_accessed` updated

**Confidence** is stored as Beta(alpha, beta) parameters. The API exposes only `confidence_expected` and `confidence_effective` (after decay). Atoms below 0.05 effective confidence are deactivated by the background consolidation job.

**Auth model (RBAC-Lite):** Three key types — `X-Admin-Key` (platform admin), `X-Operator-Key` (management-plane: register agents, rotate keys), `X-Agent-Key` (data-plane: remember, recall, share). Operators own agents; agent names are unique per operator.

**Trust model:** Directional trust rows gate sharing. Agent B can only recall memories shared by agent A if `agent_trust(agent_uuid=B, trusted_sender_uuid=A)` exists. Same-org agents get auto-seeded trust on creation.
