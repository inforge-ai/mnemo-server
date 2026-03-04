# Mnemo

**Mnemo** is a persistent, permissioned, portable memory server for AI agents. It stores typed memory atoms with semantic retrieval, knowledge graph relationships, and decay dynamics.

## Status

All core phases are complete and passing 125 tests:

| Feature | Status |
|---|---|
| FastAPI server + DB pool | Done |
| Rule-based decomposer (episodic/semantic/procedural) | Done |
| Arc decomposer (structural summary atoms) | Done |
| Atom storage, embedding, duplicate detection | Done |
| Semantic recall with confidence decay | Done |
| Knowledge graph edges + expansion | Done |
| Views and snapshot sharing | Done |
| Capabilities / permission grants | Done |
| Background consolidation (decay, clustering, merge, purge) | Done |
| MCP server (Claude tool interface) | Done |
| Multi-agent MCP support | Done |
| Mock agent simulation framework | Done |

Not yet built (v0.1 scope exclusions): authentication/API keys, rate limiting, live view subscriptions, LLM-based decomposition, contradiction detection, horizontal scaling, any UI.

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

## Working with Agents

### Register an agent

An agent is a named identity with an optional persona and domain tags. You must register an agent before storing or retrieving memories.

```bash
curl -s -X POST http://localhost:8000/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-agent",
    "persona": "A Python backend developer",
    "domain_tags": ["python", "backend"],
    "metadata": {}
  }'
```

Response:

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "my-agent",
  "persona": "A Python backend developer",
  "domain_tags": ["python", "backend"],
  "metadata": {},
  "created_at": "2026-03-04T12:00:00Z",
  "is_active": true
}
```

Save the `id` — you need it for all subsequent calls.

### Store a memory

Submit free text. The server decomposes it into typed atoms (episodic, semantic, procedural), generates embeddings, deduplicates, and links related atoms automatically.

```bash
AGENT_ID="550e8400-e29b-41d4-a716-446655440000"

curl -s -X POST http://localhost:8000/v1/agents/$AGENT_ID/remember \
  -H "Content-Type: application/json" \
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
  -d '{
    "query": "which database library should I use with FastAPI?",
    "max_results": 5,
    "expand_graph": true
  }'
```

### Get agent stats

```bash
curl -s http://localhost:8000/v1/agents/$AGENT_ID/stats
```

### Agent departure

Marks an agent as departed, cascade-revokes all capabilities it granted, and schedules its data for deletion after 30 days.

```bash
curl -s -X POST http://localhost:8000/v1/agents/$AGENT_ID/depart
```

## MCP Server (Claude integration)

Mnemo ships an MCP server so Claude can use it as a memory tool directly.

```bash
# Set env vars then run
MNEMO_AGENT_ID=$AGENT_ID python -m mnemo.mcp.mcp_server
```

Tools exposed: `mnemo_remember`, `mnemo_recall`, `mnemo_stats`.

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
│   ├── decomposer.py     # rule-based free-text → typed atoms + arc synthesis
│   └── routes/           # agents, memory, atoms, views, capabilities
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
5. On `/recall`: query embedded → vector similarity search → confidence decay applied → knowledge graph optionally expanded → `last_accessed` updated

**Confidence** is stored as Beta(α, β) parameters. The API exposes only `confidence_expected` and `confidence_effective` (after decay). Atoms below 0.05 effective confidence are deactivated by the background consolidation job.
