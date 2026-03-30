# Mnemo Server Architecture

## Overview

Mnemo is a persistent, permissioned memory server for AI agents. It stores typed memory atoms with semantic retrieval, Bayesian confidence scoring, knowledge graph relationships, temporal decay, and capability-controlled sharing between agents.

The system is built on three core abstractions:

1. **Atoms** -- discrete knowledge claims with type, confidence, and embeddings
2. **Graphs** -- typed edges connecting atoms within and across memory calls
3. **Views** -- scoped, frozen projections of an agent's atom store, shareable via capabilities

## System Organization

```
mnemo/
  server/
    main.py                 # FastAPI app, lifespan, consolidation scheduling
    config.py               # pydantic-settings (MNEMO_ env prefix)
    database.py             # asyncpg connection pool
    models.py               # Pydantic request/response models
    embeddings.py           # EmbeddingGemma-300M (768-dim), thread-pool encode
    decomposer.py           # Rule-based regex decomposer (fallback)
    llm_decomposer.py       # Claude Haiku 4.5 structured extraction
    auth.py                 # Operator/agent/admin auth context
    routes/
      memory.py             # /remember, /recall endpoints
      agents.py             # Agent registration, key rotation
      views.py              # View creation, shared recall
      shares.py             # Sharing convenience endpoints
      capabilities.py       # Capability grant/revoke
      health.py             # Health check (public + admin detailed)
      auth.py               # Operator auth (whoami, key rotation)
      admin.py              # Admin auth middleware
      admin_operators.py    # Admin operator CRUD
      admin_agents.py       # Admin agent management
      admin_trust.py        # Global sharing toggle
      atoms.py              # Direct atom CRUD (operator-level)
    services/
      atom_service.py       # Store and retrieve logic (core business layer)
      graph_service.py      # Recursive CTE graph expansion
      view_service.py       # Snapshot creation, shared recall
      consolidation.py      # Background decay/cluster/merge pipeline
      agent_service.py      # Departure, reinstatement
      auth_service.py       # Key hashing, operator creation
      address_service.py    # Agent address resolution
      migration_service.py  # Schema migration auto-detection
  client/
    __init__.py             # MnemoClient (httpx async client)
  mcp/
    server.py               # MCP tool wrapper (FastMCP, stdio/SSE transport)
  cli.py                    # Click CLI (operator + admin commands)
```

## Graphs in the System

Mnemo maintains two distinct graph structures that serve different purposes.

### Knowledge Graph (Persistent)

The knowledge graph is stored in the `edges` table and represents typed relationships between atoms. Edges are created during three operations:

**Within-call edges** -- When multiple atoms are stored in a single `/remember` call, pairwise cosine similarity is computed. Pairs above 0.70 get a `related` edge with the similarity as weight.

**Cross-call edges** -- Each newly inserted atom is compared against the agent's existing atoms via ANN (approximate nearest neighbor) search. Neighbors above the `cross_call_edge_threshold` (default: 0.55) get `related` edges. This connects knowledge across separate memory sessions.

**Consolidation edges** -- The background consolidation pipeline creates `generalises` edges from synthetic semantic atoms to the episodic cluster members they summarize.

**Structural edges** -- Arc atoms (multi-sentence structural summaries created by the decomposer) get `summarises` edges to their component atoms.

Edge types in the schema: `related`, `causal`, `temporal_sequence`, `supports`, `contradicts`, `depends_on`, `generalises`, `specialises`, `motivated_by`, `evidence_for`, `supersedes`, `summarises`.

### Recall DAG (Transient)

During retrieval, a directed acyclic graph is constructed on the fly via a PostgreSQL recursive CTE. This DAG is never persisted -- it exists only for the duration of a single recall query.

The DAG starts from seed atoms (the primary similarity search results) and expands outward through the knowledge graph edges, up to a configurable depth (default: 2 hops). At each hop, relevance decays by `weight * 0.7`, ensuring that distant atoms contribute less to results.

The recall DAG is scope-bounded in two ways:
- **Private recall**: expansion is restricted to the querying agent's own atoms (`agent_id` filter)
- **Shared recall**: expansion is restricted to the atoms frozen in the view's `snapshot_atoms` table (`allowed_ids` filter), preventing any edge from pulling atoms outside the granted scope

## Data Flow: Remember

See [diagrams/remember.mmd](diagrams/remember.mmd) for the full flow diagram.

### Summary

1. Agent POSTs free text to `/agents/{agent_id}/remember`
2. Server validates auth, creates a store job, and returns `201 Created` with a `store_id` immediately
3. Background task decomposes text into typed atoms via Claude Haiku 4.5 (with regex fallback)
4. Each atom is embedded with EmbeddingGemma-300M (768-dim, `prompt_name="document"`)
5. Duplicate detection: cosine similarity > 0.90 against same agent/type triggers Bayesian merge (`alpha_new = alpha_old + alpha_incoming - 1`) instead of insertion
6. New atoms are inserted with type-specific decay half-lives (episodic: 14d, semantic: 90d, procedural: 180d)
7. Within-call edges created between atoms with similarity > 0.70
8. Cross-call edges link new atoms to existing atoms above threshold (0.55)
9. Store job marked complete with atom count

### Decomposition

The LLM decomposer (Claude Haiku 4.5) extracts atoms with:
- Type classification (episodic/semantic/procedural)
- Confidence scoring mapped to Beta distribution bands
- Mandatory absolute date resolution from `remembered_on` context
- Proper noun and named entity preservation

The regex fallback uses pattern matching for type classification (imperative markers for procedural, past-tense first-person for episodic, default semantic) and keyword-based confidence inference.

### Duplicate Merge

When a new atom matches an existing atom (cosine > 0.90, same agent, same type), evidence is accumulated rather than creating a duplicate:

```
alpha_new = alpha_old + alpha_incoming - 1    (subtract shared prior)
beta_new  = beta_old  + beta_incoming  - 1
```

For a typical high-confidence duplicate with incoming Beta(8,1), this adds 7 to alpha per repetition.

## Data Flow: Recall

See [diagrams/recall.mmd](diagrams/recall.mmd) for the full flow diagram.

### Summary

1. Agent POSTs query to `/agents/{agent_id}/recall`
2. Query embedded with EmbeddingGemma-300M (`prompt_name="query"`)
3. Vector similarity search via ivfflat index, over-fetching `max_results * 2` candidates
4. `effective_confidence()` computed inline in SQL (decay applied at query time, never stored)
5. Python-side filtering: similarity floor, confidence floor, superseded atom removal, deduplication (cosine > 0.95)
6. Composite scoring: `similarity * (0.7 + 0.3 * confidence_effective)`, with 15% penalty for consolidated atoms
7. Gap threshold applied: results cut when score drops > 15% between consecutive atoms
8. Graph expansion via recursive CTE (depth-bounded, scope-bounded)
9. Expanded atoms scored against query with permissive floor (60% of primary floor)
10. `last_accessed` and `access_count` updated on all returned atoms (both primary and expanded)

### Effective Confidence

Decay is computed at query time by a PostgreSQL function:

```
c_eff = (alpha / (alpha + beta)) * decay_factor

decay_factor = 0.5 ^ (age_days / (half_life * (1 + 0.1 * ln(1 + access_count))))
```

The access bonus term extends the effective half-life for frequently retrieved atoms, implementing retrieval-strengthened memory traces.

### Composite Score

```
S = similarity * (0.7 + 0.3 * c_eff)
```

Consolidated atoms (from the background consolidation pipeline) receive a 15% penalty (`S *= 0.85`) because their centroid embeddings are broadly similar to many queries, over-matching relative to their actual specificity.

## Data Flow: Shared Recall

See [diagrams/recall_shared.mmd](diagrams/recall_shared.mmd) for the full flow diagram.

### View Creation

An agent creates a view by specifying an `atom_filter` (query, atom types, domain tags). The server:
1. Evaluates the filter against the agent's current atoms
2. Creates a `views` record with the filter as JSONB
3. Freezes matching atom IDs into `snapshot_atoms` -- this is the immutable scope boundary

### Capability Grant

The grantor agent grants a capability on the view to a grantee agent. The system validates:
- Grantor owns the view
- Global sharing is enabled
- Operator sharing scope permits the grant (none/intra/full)
- Grantee exists and is active

Capabilities can be chained: a grantee can re-grant via `parent_cap_id`, creating a capability tree.

### Shared Recall

When a grantee calls `recall_shared`:
1. Capability validated (non-revoked, non-expired, non-blocked)
2. Bidirectional trust verified (`agent_trust` table)
3. Atoms retrieved from `snapshot_atoms` join -- only frozen atom IDs are queryable
4. Graph expansion bounded by `allowed_ids = snapshot_atoms` -- edges cannot escape the view scope
5. Access logged with capability ID for audit

### Cascade Revocation

Revoking a capability cascades to all descendants via recursive CTE on `parent_cap_id`. Agent departure triggers `revoke_agent_capabilities()`, a PostgreSQL function that revokes all capabilities granted by the departing agent and their transitive children.

### Multi-View Recall

`recall_all_shared` queries across all views shared with a grantee in a single operation, joining through `capabilities -> views -> snapshot_atoms -> atoms` with trust validation per grantor.

## Background Consolidation

The consolidation pipeline runs every 60 minutes (configurable) under a PostgreSQL advisory lock. Five steps execute in separate transactions:

1. **Decay reaping** -- Deactivate atoms with `effective_confidence < 0.05`
2. **Clustering** -- Find connected components of 3+ episodic atoms (same agent, overlapping domain tags, cosine > 0.85) via union-find. Create generalised semantic atoms with centroid embeddings and `generalises` edges to members
3. **Duplicate merging** -- Merge active atom pairs (same agent/type, cosine > 0.90) via Bayesian update. Older atom survives; edges reassigned; merge logged
4. **Edge pruning** -- Delete edges where either endpoint is inactive
5. **Agent purging** -- Delete departed agents past `data_expires_at` (default: 30 days retention)

## Auth Model

Three tiers of authentication:

- **Admin** (`X-Admin-Key` header): Platform operations -- operator CRUD, agent management, trust toggle
- **Operator** (`X-Operator-Key` header or `Authorization: Bearer`): Agent registration, key rotation, atom access
- **Agent** (`X-Agent-Key` header): Memory operations (remember, recall, share)

Agent keys are SHA-256 hashed at rest. Operator keys use the same scheme. The admin key is compared directly from configuration.

## Configuration

All settings via `MNEMO_` environment prefix (pydantic-settings):

| Setting | Default | Purpose |
|---------|---------|---------|
| `MNEMO_EMBEDDING_MODEL` | `google/embeddinggemma-300m` | Embedding model |
| `MNEMO_EMBEDDING_DIM` | `768` | Embedding dimensions |
| `MNEMO_DUPLICATE_SIMILARITY_THRESHOLD` | `0.90` | Dedup cosine threshold |
| `MNEMO_CROSS_CALL_EDGE_THRESHOLD` | `0.55` | Cross-call edge threshold |
| `MNEMO_CONSOLIDATION_INTERVAL_MINUTES` | `60` | Consolidation frequency |
| `MNEMO_MIN_EFFECTIVE_CONFIDENCE` | `0.05` | Decay reaping floor |
| `MNEMO_DECAY_EPISODIC` | `14.0` | Episodic half-life (days) |
| `MNEMO_DECAY_SEMANTIC` | `90.0` | Semantic half-life (days) |
| `MNEMO_DECAY_PROCEDURAL` | `180.0` | Procedural half-life (days) |
| `MNEMO_DEPARTURE_RETENTION_DAYS` | `30` | Post-departure data retention |
