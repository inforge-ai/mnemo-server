# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Mnemo** is a persistent, permissioned, portable memory server for AI agents. It stores typed memory atoms with semantic retrieval, knowledge graph relationships, decay dynamics, and view sharing/skill export.

This is a greenfield project. The full implementation spec is in `docs/mnemo_implementation_spec.md` — read it before implementing anything.

## Package Management

Use `uv` exclusively. Never use pip directly.

```bash
uv add <package>          # add dependency
uv run <command>          # run in project venv
uv run pytest             # run all tests
uv run pytest tests/test_remember.py  # run a single test file
uv run pytest -k "test_name"          # run a specific test
uv run uvicorn mnemo.server.main:app --reload  # dev server
```

Python version: 3.12 (see `.python-version`).

## Database Setup

PostgreSQL 16 with pgvector is required:

```bash
sudo apt install postgresql-16 postgresql-16-pgvector
sudo -u postgres createdb mnemo
sudo -u postgres psql mnemo -c "CREATE EXTENSION IF NOT EXISTS vector;"
sudo -u postgres psql mnemo -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"
```

Connection: `postgresql://USER:PASSWORD@HOST:PORT/DB_NAME`. Override via `MNEMO_DATABASE_URL` env var.

## Architecture

### Planned Project Structure

```
mnemo/
├── server/
│   ├── main.py           # FastAPI app, lifespan, consolidation scheduling
│   ├── config.py         # pydantic-settings (MNEMO_ env prefix)
DB_NAME   ├── database.py       # asyncpg connection pool
│   ├── models.py         # Pydantic request/response models
│   ├── embeddings.py     # sentence-transformers (all-MiniLM-L6-v2, 384-dim)
│   ├── decomposer.py     # rule-based free-text → typed atoms
│   └── routes/           # agents, memory, atoms, views, capabilities
│   └── services/         # atom_service, graph_service, view_service, consolidation
├── client/mnemo_client.py  # httpx async client
├── mcp/mcp_server.py       # MCP wrapper
└── tests/
```

### Core Data Flow

**Store (`/remember`):** Free-text → `decomposer` classifies sentences into typed atoms (episodic/semantic/procedural) with inferred Beta-distribution confidence → embedding generated → duplicate detection (cosine > 0.90 merges instead of creating) → atoms stored → edges created between co-submitted atoms → embeddings indexed with ivfflat.

**Retrieve (`/recall`):** Query → embedding → vector similarity search filtered by `effective_confidence()` SQL function (decay applied at query time) → `last_accessed` and `access_count` updated → optional scope-bounded graph expansion via recursive CTE.

**Confidence:** Stored as Beta(α, β) parameters. Never exposed as raw parameters via API — only `confidence_expected` (α/(α+β)) and `confidence_effective` (after decay) are returned.

**Decay:** `effective_confidence()` is a PostgreSQL function that applies exponential/linear decay based on age and access count. Atoms below 0.05 effective confidence are deactivated by the background consolidation job.

**Views/Skills:** Snapshots freeze atom IDs into `snapshot_atoms` at creation time (immune to later decay). Only `α=1` (full-fidelity skill export) in v0.1. Graph expansion within shared views is scope-bounded — edges cannot pull atoms outside the view's filter.

**Capabilities:** Agent departure cascade-revokes all granted capabilities via `revoke_agent_capabilities()` SQL function (recursive CTE).

### Design Principles (from spec — these take priority)

1. **Simple interface, rich internals.** Agents submit free text to `/remember`. Server handles classification, typing, and linking.
2. **Confidence is inferred, not declared.** Server assigns Beta parameters from linguistic cues. Agents never specify them.
3. **Decay is real.** `effective_confidence()` is used in every retrieval. Access timestamps are updated on every retrieval.
4. **Views are safe by construction.** Graph expansion within a shared view never returns atoms outside the view's filter scope.
5. **Snapshots only for v0.1.** No live subscriptions.
6. **Departure revokes everything.** Cascade revoke on departure is non-optional.

### What NOT to build in v0.1

Authentication/API keys, rate limiting, live subscriptions, LLM-based decomposition, contradiction detection, α≠1 projections, horizontal scaling, any UI. See spec §9.

## Key Configuration

All settings via `MNEMO_` env prefix (see `server/config.py`):
- Embedding model: `all-MiniLM-L6-v2` (384-dim, local, no API calls)
- Duplicate threshold: cosine similarity > 0.90
- Consolidation interval: 60 minutes
- Departure data retention: 30 days
- Decay half-lives: episodic=14d, semantic=90d, procedural=180d
