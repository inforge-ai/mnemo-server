# Mnemo

**Mnemo** is a persistent, permissioned, portable memory server for AI agents. It stores typed memory atoms with semantic retrieval, knowledge graph relationships, decay dynamics, and view sharing/skill export.

## Status

All core phases are complete and passing 183 tests:

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
| Background consolidation (decay, clustering, merge, purge) | Done |
| Operator-scoped authentication (API keys, ownership) | Done |
| Admin API (agents, operations, keys, glance) | Done |
| CLI (register-operator, create-agent, list-agents, new-key, whoami) | Done |
| MCP server (Claude tool interface) | Done |
| Multi-agent MCP support | Done |
| Mock agent simulation framework | Done |

Not yet built (v0.1 scope exclusions): rate limiting, live view subscriptions, LLM-based decomposition, contradiction detection, horizontal scaling, skill files, any UI.

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

Default connection: `postgresql://mnemo:mnemo@localhost:5432/mnemo`. Override via `MNEMO_DATABASE_URL`.

### Install and run

```bash
uv run uvicorn mnemo.server.main:app --reload
```

## Authentication

Mnemo uses operator-scoped authentication. An **operator** owns one or more **agents** and authenticates with an API key.

Auth is controlled by `MNEMO_AUTH_ENABLED` (default: `false`). When disabled, a "local" operator is auto-created and all agents are assigned to it.

### CLI quickstart

```bash
# Register a new operator (returns an API key)
uv run python -m mnemo.cli register-operator "My Org"

# Set the key for subsequent commands
export MNEMO_API_KEY="mnemo_..."

# Check identity
uv run python -m mnemo.cli whoami

# Create an agent under your operator
uv run python -m mnemo.cli create-agent my-agent --persona "A Python developer" --tags python,backend

# List your agents
uv run python -m mnemo.cli list-agents

# Rotate your API key
uv run python -m mnemo.cli new-key
```

### REST auth endpoints

- `POST /auth/register-operator` — register a new operator (returns API key)
- `POST /auth/new-key` — rotate your API key (Bearer token required)
- `GET /auth/me` — return current operator info

When auth is enabled, all agent/memory endpoints require a `Bearer` token and enforce ownership (an operator can only access its own agents).

## Working with Agents

### Register an agent

```bash
curl -s -X POST http://localhost:8000/v1/agents \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MNEMO_API_KEY" \
  -d '{
    "name": "my-agent",
    "persona": "A Python backend developer",
    "domain_tags": ["python", "backend"],
    "metadata": {}
  }'
```

Save the `id` from the response — you need it for all subsequent calls.

### Store a memory

Submit free text. The server decomposes it into typed atoms (episodic, semantic, procedural), generates embeddings, deduplicates, and links related atoms automatically.

```bash
AGENT_ID="550e8400-e29b-41d4-a716-446655440000"

curl -s -X POST http://localhost:8000/v1/agents/$AGENT_ID/remember \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MNEMO_API_KEY" \
  -d '{
    "text": "I learned that asyncpg is faster than psycopg2 for async workloads. I switched our connection pool to asyncpg today. Always use asyncpg for FastAPI projects.",
    "domain_tags": ["python", "databases"]
  }'
```

### Recall memories

Retrieve relevant memories via semantic search, filtered by confidence and similarity, with optional knowledge graph expansion.

```bash
curl -s -X POST http://localhost:8000/v1/agents/$AGENT_ID/recall \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MNEMO_API_KEY" \
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
curl -s http://localhost:8000/v1/agents/$AGENT_ID/stats \
  -H "Authorization: Bearer $MNEMO_API_KEY"
```

### Agent departure

Marks an agent as departed, cascade-revokes all capabilities it granted, and schedules its data for deletion after 30 days.

```bash
curl -s -X POST http://localhost:8000/v1/agents/$AGENT_ID/depart \
  -H "Authorization: Bearer $MNEMO_API_KEY"
```

## MCP Server (Claude integration)

Mnemo ships an MCP server so Claude can use it as a memory tool directly. Agent identity is resolved by name (unique per operator).

```bash
export MNEMO_BASE_URL="http://localhost:8000"
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
├── client/mnemo_client.py  # async httpx client
├── mcp/mcp_server.py       # MCP wrapper for Claude
├── simulation/             # mock agent framework for testing memory dynamics
└── tests/
```

**Core data flow:**

1. Agent submits free text to `/remember`
2. Decomposer classifies sentences into typed atoms (episodic, semantic, procedural) with inferred confidence (Beta distribution)
3. Embeddings generated; duplicates (cosine > 0.90) are merged rather than stored
4. Atoms stored; edges created between co-submitted atoms; arc atom summarises the batch
5. On `/recall`: query embedded -> vector similarity search -> confidence decay applied -> knowledge graph optionally expanded -> `last_accessed` updated

**Confidence** is stored as Beta(alpha, beta) parameters. The API exposes only `confidence_expected` and `confidence_effective` (after decay). Atoms below 0.05 effective confidence are deactivated by the background consolidation job.

**Auth model:** API key -> operator -> [agents]. Each operator has one API key and owns zero or more agents. Agent names are unique per operator.
