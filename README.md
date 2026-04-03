# Mnemo

[![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)
[![LoCoMo](https://img.shields.io/badge/LoCoMo-82.1%25-brightgreen.svg)](https://github.com/snap-stanford/locomo)

## Benchmark: LoCoMo Long-Term Conversational Memory

Mnemo scores **82.1% overall** on the [LoCoMo benchmark](https://github.com/snap-stanford/locomo) (1,540 questions across 10 long-running conversations), placing **#2 overall** and **#1 in multi-hop reasoning** — the category that requires synthesizing information across separate conversation sessions.

| Category | Correct | Total | Accuracy |
|----------|---------|-------|----------|
| **Overall** | **1,264** | **1,540** | **82.1%** |
| Temporal | 696| 841 | 82.8% |
| Multi-hop | 273 | 321 | 85.0% |
| Single-hop | 230 | 282 | 81.6% |
| Open-domain | 65 | 96 | 67.7% |

Evaluated with Claude Sonnet 4.6 as both answer model and judge. Multi-hop performance (85.0%) surpasses all published baselines.

---

## What is Mnemo?

**Persistent, shareable memory for AI agents.**

Mnemo gives AI agents the ability to remember across conversations and share knowledge with other agents through a trust-based sharing model. It stores memories as typed atoms — episodic, semantic, and procedural — with Bayesian confidence scoring, so agents build durable, queryable knowledge over time rather than starting from scratch each session.

Built for developers integrating memory into agent workflows, multi-agent systems, and MCP-compatible clients like Claude Desktop.

## Why Mnemo?

Most agent memory solutions treat memory as flat document storage or simple key-value retrieval. Mnemo is different:

- **Typed atoms** — Memories are decomposed into semantic (facts), episodic (experiences), and procedural (how-to) atoms, each with distinct decay characteristics and retrieval behavior.
- **Bayesian confidence** — Every atom carries a Beta-distribution confidence score that updates with reinforcement and decays over time, so agents can reason about how much to trust a memory.
- **Agent-to-agent sharing** — Agents can share scoped memory views with other agents through a capability-based trust model. The trust gate is enforced at the database level — untrusted content never leaves Postgres.
- **Async decomposition** — Conversations are acknowledged immediately and decomposed into atoms in the background by an LLM decomposer (Claude Haiku 4.5), so writes never block.
- **Composite retrieval** — Vector similarity search blended with confidence scoring, configurable deduplication, knowledge graph expansion, domain filtering, and verbosity controls.
- **Knowledge graph** — Atoms are linked through typed edges (supports, contradicts, generalises, supersedes, etc.) within and across memory sessions, enabling multi-hop reasoning that pure vector search misses.

## Quick Start

### Prerequisites

- Python 3.12+
- PostgreSQL 16 with [pgvector](https://github.com/pgvector/pgvector)
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (recommended for deployment)

### 1. Database Setup

```bash
sudo apt install postgresql-16 postgresql-16-pgvector
sudo -u postgres createdb mnemo
sudo -u postgres psql mnemo -c "CREATE EXTENSION IF NOT EXISTS vector;"
sudo -u postgres psql mnemo -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'
sudo -u postgres psql mnemo -f schema.sql
```

### 2. Configuration

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

**Required settings:**

| Variable | Purpose |
|----------|---------|
| `MNEMO_DATABASE_URL` | PostgreSQL connection string |
| `MNEMO_ADMIN_KEY` | Admin authentication key (generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) |

**Recommended:**

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Enables LLM decomposer (Claude Haiku 4.5). Without it, falls back to regex. |
| `HF_TOKEN` | Required for the gated EmbeddingGemma-300M embedding model |

### 3. Run with Docker

```bash
BUILD_COMMIT=$(git rev-parse HEAD) docker compose build
docker compose up -d
```

### 4. Verify

```bash
curl -s http://localhost:8000/v1/health | python3 -m json.tool
```

You should see `"status": "ok"` with Postgres connected and the embedding model loaded.

### 5. Create an Operator and Agent

```bash
# Set your admin key
export MNEMO_ADMIN_TOKEN="<your MNEMO_ADMIN_KEY value>"
export MNEMO_BASE_URL="http://localhost:8000"

# Create an operator (returns a one-time API key)
mnemo admin operator create \
  --username jdoe --org acme \
  --display-name "Jane Doe" --email jane@acme.com

# Set the operator key
export MNEMO_API_KEY="mnemo_..."  # from the output above

# Create an agent (returns a one-time agent key)
mnemo create-agent my-agent --persona "A Python developer" --tags python,backend

# Verify
mnemo whoami
mnemo list-agents
```

### 6. Store and Recall

```bash
export AGENT_ID="..."       # from create-agent output
export MNEMO_AGENT_KEY="..."  # from create-agent output

# Store a memory
curl -s -X POST http://localhost:8000/v1/agents/$AGENT_ID/remember \
  -H "Content-Type: application/json" \
  -H "X-Agent-Key: $MNEMO_AGENT_KEY" \
  -d '{"text": "asyncpg is faster than psycopg2 for async workloads. Always use asyncpg with FastAPI."}'

# Recall
curl -s -X POST http://localhost:8000/v1/agents/$AGENT_ID/recall \
  -H "Content-Type: application/json" \
  -H "X-Agent-Key: $MNEMO_AGENT_KEY" \
  -d '{"query": "which database library for FastAPI?"}' | python3 -m json.tool
```

## MCP Integration

Mnemo ships an MCP server for use with Claude Desktop and other MCP-compatible clients, through the Mnemo client found in the mnemo-ai repository, available on PyPI.

Tools exposed: `mnemo_remember`, `mnemo_recall`, `mnemo_recall_shared`, `mnemo_share`, `mnemo_list_shared`, `mnemo_revoke_share`, `mnemo_stats`.

## Project Structure

```
mnemo/
  server/
    main.py               FastAPI app, lifespan, consolidation scheduling
    config.py              pydantic-settings (MNEMO_ env prefix)
    database.py            asyncpg connection pool
    embeddings.py          EmbeddingGemma-300M (768-dim)
    llm_decomposer.py      Claude Haiku 4.5 structured extraction
    decomposer.py          Rule-based regex decomposer (fallback)
    auth.py                Three-tier auth (admin / operator / agent)
    routes/                API endpoints
    services/              Business logic
  client/                  Async Python client (httpx)
  mcp/                     MCP server for Claude Desktop
  cli.py                   CLI for operator and admin management
```

## Documentation

- [Architecture Overview](docs/architecture.md) — system design, data flows, knowledge graph
- [Technical Reference](docs/technical_reference.md) — full API documentation, database schema, configuration
- [Mermaid Diagrams](docs/diagrams/) — visual flows for remember, recall, shared recall, capability DAG

## License

Mnemo Server is licensed under the [Business Source License 1.1](LICENSE). See the LICENSE file for details.

## Links

- [Mnemo](https://mnemo-ai.com) — product website
- [LoCoMo Benchmark](https://github.com/snap-stanford/locomo) — long-term conversational memory benchmark
- [Architecture](docs/architecture.md) — system design documentation
- [Python Client](https://pypi.org/project/mnemo-ai/) — `pip install mnemo-ai`
- [Inforge](https://inforge-ai.com) — the team behind Mnemo

## Contact

- **Questions:** info@inforge-ai.com
- **Support:** support@mnemo-ai.com
