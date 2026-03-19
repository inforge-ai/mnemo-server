# Mnemo Technical Overview

## What Mnemo Is

Mnemo is a persistent memory server for AI agents. An agent says what happened in natural language. The server breaks that text into typed memory atoms, assigns confidence scores, detects duplicates, builds a knowledge graph, and makes it all retrievable via semantic search. Over time, memories decay, consolidate, and can be shared between agents as skill packages.

The design principle is **simple interface, rich internals**. The agent never classifies, labels, or structures its own memories. It just talks. The server does the remembering.

---

## Atoms: The Unit of Memory

An **atom** is the fundamental unit of memory in Mnemo. Every piece of knowledge stored in the system exists as an atom in the `atoms` table.

### Atom Types

Each atom has exactly one type, assigned by the server during decomposition:

| Type | What It Represents | Decay Half-Life | Example |
|------|-------------------|-----------------|---------|
| **episodic** | A first-person experience — something the agent did, saw, or encountered | 14 days | "I discovered silent type coercion processing client_data.csv when row 847 had a string in account_id" |
| **semantic** | A general fact about how the world works | 90 days | "pandas.read_csv silently coerces mixed-type columns" |
| **procedural** | A rule, practice, or instruction for future behavior | 180 days | "Always specify dtype explicitly when using read_csv" |
| **relational** | A relationship between entities (defined in schema, not currently produced by the decomposer) | 90 days | — |

Episodic memories decay fastest because specific experiences lose relevance quickly. Procedural knowledge persists longest because learned practices remain valuable.

### Atom Fields

Every atom carries:

- **text_content**: The human-readable memory text
- **embedding**: A 384-dimensional vector (GTE-small model) for semantic search
- **confidence_alpha / confidence_beta**: Beta distribution parameters representing certainty (see Confidence below)
- **source_type**: How the atom was created — `direct_experience`, `inference`, `shared_view`, `imported_skill`, `consolidation`, or `arc`
- **domain_tags**: Topic labels (e.g., `["python", "pandas"]`) inherited from the `/remember` call
- **structured**: Optional JSONB for extracted code snippets
- **decay_type**: `exponential` (default), `linear`, or `none`
- **decay_half_life_days**: How quickly this atom fades (set by type)
- **last_accessed / access_count**: Updated on every retrieval — frequently accessed memories decay slower
- **is_active**: Soft-delete flag; atoms below the confidence floor are deactivated by consolidation
- **decomposer_version**: Tracks which decomposer variant produced the atom

### Arc Atoms

When an agent submits 3+ sentences in a single `/remember` call, the decomposer creates individual typed atoms AND an additional **arc atom** that preserves the holistic structure. This prevents the decomposer from destroying the causal arc of multi-sentence narratives.

- **3-6 sentences**: Arc atom contains the full original text, unmodified
- **7+ sentences**: Arc atom contains a compressed version — the first sentence (context), the longest sentence (information-dense), and the last sentence (conclusion)
- Arc atoms are typed as `episodic` with `source_type = "arc"` and moderate confidence Beta(4, 2)
- Arc atoms create `summarises` edges to every non-arc atom from the same call
- Arc atoms **never deduplicate** — their embeddings naturally overlap with component atoms by design

---

## The Decomposer: Free Text to Typed Atoms

The decomposer is the core innovation. It takes freeform text from `/remember` and produces typed atoms with inferred confidence and inter-atom edges.

### Two Decomposer Implementations

**Rule-based decomposer** (`decomposer.py`): Always available. Uses regex pattern matching for type classification and confidence inference. Fast, deterministic, no external dependencies.

**LLM decomposer** (`llm_decomposer.py`): Activated when `ANTHROPIC_API_KEY` is set. Uses Claude Haiku to extract discrete facts from text and assign confidence scores. System prompt is cached (5-minute window) for efficiency. All LLM-decomposed atoms are typed as `semantic` — the LLM judges information quality, not category.

### Decomposition Pipeline (Rule-Based)

```
Input text
    │
    ▼
Split into sentences (protecting code blocks and dotted identifiers)
    │
    ▼
Filter out fragments < 10 characters
    │
    ▼
For each sentence:
    ├── Classify type (procedural → episodic → semantic)
    ├── Infer confidence (Beta parameters from linguistic cues)
    └── Extract structured data (inline code)
    │
    ▼
Merge adjacent atoms of same type
    │
    ▼
If 3+ sentences: create arc atom
    │
    ▼
Infer edges between atoms
    │
    ▼
Return DecomposedAtom list + edge list
```

### Type Classification Rules

The decomposer checks **procedural first** (stronger signal), then episodic, defaulting to semantic:

**Procedural** — imperative or prescriptive language:
- "always", "never", "should", "must"
- "when X, do Y" patterns
- "to prevent/avoid/fix"
- "best practice", "pro tip", "lesson learned"

**Episodic** — first-person experience with temporal context:
- "I found/discovered/noticed/tried"
- "today", "yesterday", "while working"

**Semantic** — everything else (default). General factual statements.

### Confidence Inference

Confidence is stored as a Beta distribution Beta(α, β). The expected value is α/(α+β). The server infers parameters from linguistic cues:

| Signal | α | β | Expected Confidence |
|--------|---|---|-------------------|
| Episodic (direct experience) | 8.0 | 1.0 | 0.889 |
| Procedural/semantic (default) | 4.0 | 2.0 | 0.667 |
| High-confidence words ("confirmed", "verified", "tested") | 8.0 | 1.0 | 0.889 |
| Hedging ("I think", "maybe", "possibly") | 2.0 | 3.0 | 0.400 |
| Strong uncertainty ("I don't know if", "could be wrong") | 2.0 | 4.0 | 0.333 |

**Why Beta distributions?** They naturally represent "evidence for" (α) and "evidence against" (β). When duplicate atoms are detected, Bayesian updating adds evidence: `α_new = α_old + α_incoming - 1`. This means reinforcing a memory (storing similar knowledge again) increases its confidence without ever resetting it.

### Edge Inference

Atoms from the same `/remember` call are linked by typed edges:

- **episodic → semantic**: `evidence_for` (the experience supports the fact)
- **procedural → semantic**: `motivated_by` (the rule is motivated by the fact)
- **episodic → procedural**: `evidence_for` (when no semantic atom is present)
- **arc → all non-arc atoms**: `summarises`

Additionally, during storage, atoms from the same call with cosine similarity > 0.7 get `related` edges.

---

## The Knowledge Graph

Edges connect atoms into a knowledge graph. Each edge has a type, a weight (0.0–1.0), and directionality.

### Edge Types

| Edge Type | Meaning | Created By |
|-----------|---------|------------|
| `supports` | Source provides evidence for target | Manual |
| `contradicts` | Source contradicts target | Manual |
| `depends_on` | Source depends on target | Manual |
| `generalises` | Source is a generalization of target | Consolidation |
| `specialises` | Source is a specialization of target | Manual |
| `motivated_by` | Source is motivated by target | Decomposer |
| `evidence_for` | Source provides evidence for target | Decomposer |
| `supersedes` | Source replaces target (target filtered from retrieval) | Manual |
| `summarises` | Source summarizes target | Decomposer (arc atoms) |
| `related` | General semantic relationship | Storage (cosine > 0.7) |

### Graph Expansion

When retrieving memories, the system can follow edges from primary results to discover related atoms. This is implemented as a **recursive CTE** in PostgreSQL:

1. Start with seed atoms (the primary retrieval results)
2. Follow edges bidirectionally (both source→target and target→source)
3. At each hop, relevance decays: `relevance *= edge.weight * 0.7`
4. Continue up to configurable depth (default: 2)
5. Filter: only active atoms matching any scope constraints

**Scope-bounded expansion** is the critical safety property. When retrieving through a shared view, expansion can **never** return atoms outside the view's snapshot set. The `allowed_ids` parameter restricts the CTE to only traverse within a fixed set of atom IDs.

---

## Storage Flow (`/remember`)

```
POST /v1/agents/{agent_id}/remember
Body: {"text": "...", "domain_tags": ["python"]}
```

The remember endpoint returns immediately with a `store_id` and processes asynchronously (synchronous mode available for tests). The full flow:

1. **Decompose** text into atoms (rule-based or LLM)
2. For each atom:
   a. **Generate embedding** (GTE-small, 384-dim, via thread executor to avoid blocking)
   b. **Duplicate detection**: search for existing atoms with cosine > 0.90, same agent, same type
   c. If duplicate found: **Bayesian merge** — update α, update last_accessed, increment access_count. No new atom created.
   d. If no duplicate: **insert** new atom with server-assigned decay parameters based on type
   e. Arc atoms skip duplicate detection entirely
3. **Create edges** between atoms from the same call:
   - Decomposer-inferred edges (evidence_for, motivated_by, summarises)
   - Similarity-based `related` edges for pairs with cosine > 0.7
4. Return: `{atoms_created, edges_created, duplicates_merged}`

### Duplicate Detection in Detail

Duplicate detection prevents the knowledge base from filling with near-identical atoms. The threshold is cosine similarity > 0.90 (configurable via `MNEMO_DUPLICATE_SIMILARITY_THRESHOLD`).

When a duplicate is found, confidence merges via Bayesian update rather than replacement:
```
α_new = α_existing + α_incoming - 1    (clamped ≥ 1)
```

This means telling the system the same thing twice makes it more confident, not redundant.

---

## Retrieval Flow (`/recall`)

```
POST /v1/agents/{agent_id}/recall
Body: {"query": "loading CSV files with pandas", "max_results": 10, ...}
```

Retrieval is a multi-stage pipeline:

### Stage 1: Vector Search
- Generate embedding from query text
- Fetch top `2 × max_results` atoms ordered by cosine distance (ascending)
- Filter: `cosine_sim >= min_similarity` (default 0.2), `is_active = true`, domain tag overlap (if specified)
- The `effective_confidence()` PostgreSQL function is called inline to filter atoms below `min_confidence` (default 0.1)

### Stage 2: Supersession Filtering
- Exclude atoms that have an active atom pointing to them via a `supersedes` edge
- This means newer knowledge automatically hides older, superseded knowledge
- Can be bypassed with `include_superseded=True`

### Stage 3: Near-Duplicate Deduplication
- Among retrieved results, deduplicate atoms with cosine > 0.95
- Keeps the atom with higher effective confidence

### Stage 4: Ranking
- Composite score: `cosine_sim * (0.7 + 0.3 * effective_confidence)`
- This weights relevance at 70% and confidence at 30%
- Semantic match matters most, but confident atoms get a boost

### Stage 5: Gap Threshold
- After ranking, walk the results in score order
- When `(prev_score - curr_score) / prev_score > threshold` (default 0.3), stop
- This creates a natural cutoff rather than an arbitrary result count
- At least 1 atom is always returned

### Stage 6: Access Update
- Update `last_accessed = now()` and `access_count += 1` on all primary results
- This is what makes decay real — accessed memories live longer

### Stage 7: Graph Expansion (Optional)
- Use post-threshold atoms as seeds
- Expand via recursive CTE up to configured depth
- Expanded atoms have a floor: `min_similarity * 0.6`
- Score expanded atoms the same way
- Update access timestamps on expanded atoms too

### Stage 8: Token Budget
- If `max_total_tokens` is set, enforce a character budget (chars/4 ≈ tokens)
- Primary atoms get budget priority; remainder goes to expanded atoms
- At least 1 atom is always returned regardless of budget

### Stage 9: Verbosity Control
- `full`: Return atoms unchanged
- `summary`: First sentence only
- `truncated`: First N characters + "..."

---

## Confidence and Decay

### The Beta Distribution Model

Every atom's confidence is stored as Beta(α, β) parameters. The **expected confidence** is simply α/(α+β). But confidence is never static — it decays over time.

The **effective confidence** is computed at query time by the `effective_confidence()` PostgreSQL function:

```
effective = base_confidence × decay_factor
```

Where:
- `base_confidence = α / (α + β)`
- For exponential decay: `decay_factor = 0.5^(age_days / (half_life × (1 + access_bonus)))`
- For linear decay: `decay_factor = max(0, 1 - age_days / (2 × half_life × (1 + access_bonus)))`
- `access_bonus = ln(1 + access_count) × 0.1`

**Key properties:**
- Age is measured from `last_accessed` (not `created_at`), so accessing a memory resets its decay clock
- Frequently accessed memories decay slower via the access bonus
- `decay_type = 'none'` returns base confidence unchanged (used for imported skills)

### What the API Exposes

The raw α and β parameters are **never exposed** through the API. Consumers see:
- `confidence_expected`: The base α/(α+β) — what the confidence would be without decay
- `confidence_effective`: The decayed value — what confidence is right now

This hides the internal representation while giving consumers both the "original strength" and "current relevance" of a memory.

### Consolidation

A background job runs every 60 minutes (configurable) and performs:

1. **Decay**: Deactivate atoms where `effective_confidence < 0.05`
2. **Cluster & Generalise**: Find groups of 3+ similar episodic atoms (cosine > 0.85, same agent, overlapping domain tags). Create a new semantic atom with the centroid embedding and `generalises` edges to cluster members.
3. **Merge Duplicates**: Find atom pairs with cosine > 0.90 within same agent/type. Keep the older atom, Bayesian-merge confidence, reassign edges, deactivate the newer atom.
4. **Prune Dead Edges**: Delete edges where source or target is inactive.
5. **Purge Departed Agents**: Delete all data for agents whose `data_expires_at < now()`.

PostgreSQL advisory locks ensure only one consolidation runs at a time.

---

## Views, Skills, and Sharing

### Views (Snapshots)

A **view** freezes a set of atom IDs at creation time. The filter specifies which atoms to include:

```json
{"atom_types": ["procedural"], "domain_tags": ["pandas"], "query": "CSV handling", "max_atoms": 20}
```

The matching atom IDs are stored in `snapshot_atoms`. This set is **immutable** — even if source atoms later decay or are deactivated, the snapshot remembers which atoms it captured. (Current implementation note: decayed/deactivated atoms won't appear in recall results even if they're in the snapshot. True freezing is planned for a future version.)

### Skill Export

A skill export packages a view's procedural atoms plus their supporting semantic atoms (found via graph expansion within the snapshot scope) into a structured `SkillExport` with rendered markdown.

### Capabilities and Sharing

Agent A can **grant** Agent B access to a view:
```
POST /v1/agents/{agent_a_id}/grant
{"view_id": "...", "grantee_id": "agent_b_id", "permissions": ["read"]}
```

This creates a **capability** — a token that allows the grantee to recall through the view with scope-bounded expansion. Capabilities can expire and can be revoked (with cascade to any sub-granted capabilities via recursive CTE).

**Shared recall** works exactly like normal recall, but the search space is restricted to the snapshot's atom set. Graph expansion during shared recall can **never** pull atoms from outside the snapshot — this is enforced by the `allowed_ids` parameter in the expansion CTE.

**Recall across all shared views**: A single endpoint searches across all views shared with an agent, returning results with source metadata (who shared it, from which view).

### Agent Departure

When an agent departs:
1. All capabilities the agent **granted** are cascade-revoked (recursive CTE)
2. Agent is marked inactive with `data_expires_at = now() + 30 days`
3. After expiry, consolidation purges all the agent's data

Capabilities the agent **received** are unaffected — those belong to the granting agent.

---

## Embeddings

**Model**: `thenlper/gte-small` — a 384-dimensional sentence transformer. Runs locally, no API calls. Loaded once at startup and cached.

**Normalization**: Embeddings are L2-normalized (unit vectors), which means cosine similarity is equivalent to dot product.

**Indexing**: PostgreSQL ivfflat index with 100 lists, using cosine distance operator (`<=>`). This provides approximate nearest neighbor search that scales well.

**Encoding**: Runs in a thread executor to avoid blocking the async event loop.

Note: The original spec called for `all-MiniLM-L6-v2`. The implementation uses `thenlper/gte-small` (same dimensionality, generally better quality).

---

## Authentication

Authentication is optional (`MNEMO_AUTH_ENABLED`, default false).

When enabled:
- Operators register and receive API keys (format: `mnemo_` + 32-char token)
- Keys are SHA-256 hashed in the database
- Bearer token auth on all requests
- Agents are scoped to operators — an operator can only access their own agents
- Agent addresses follow the format `agent_name:operator_username.org`

When disabled, all requests succeed with an anonymous operator sentinel.

---

## What From the Original Spec Is Not Yet Implemented

| Spec Item | Status | Notes |
|-----------|--------|-------|
| Core schema (agents, atoms, edges, views, capabilities) | **Implemented** | Extended beyond spec with operators, api_keys, agent_addresses, operations, store_failures tables |
| `effective_confidence()` SQL function | **Implemented** | Matches spec exactly |
| `revoke_agent_capabilities()` SQL function | **Implemented** | Matches spec with added `revoked_at` timestamp |
| Decomposer (rule-based) | **Implemented** | Extended beyond spec with arc atoms, merging adjacent same-type atoms |
| LLM decomposer | **Implemented** | Not in original spec — added as optional enhancement using Haiku |
| `/remember` endpoint | **Implemented** | Async processing (queued), not synchronous as spec implied |
| `/recall` endpoint | **Implemented** | Extended significantly beyond spec with gap threshold, token budget, verbosity control, supersession filtering, near-duplicate dedup |
| Graph expansion (scope-bounded) | **Implemented** | Matches spec; added `allowed_ids` for hard snapshot scoping |
| Views + snapshot_atoms | **Implemented** | Matches spec; snapshot does not truly freeze atoms (decay still applies) |
| Skill export with markdown | **Implemented** | Matches spec |
| Capabilities + grant/revoke/cascade | **Implemented** | Extended with outbound capability listing, idempotent grant |
| Agent departure + cascade revoke | **Implemented** | Matches spec |
| Consolidation (decay, cluster, merge, purge) | **Implemented** | Matches spec; added advisory locking, last_consolidated_at tracking |
| Stats endpoint | **Implemented** | Extended with arc_atoms count |
| Client library | **Implemented** | Extended beyond spec with auth support, shared recall methods |
| MCP server | **Partially implemented** | Files exist but not fully documented here |
| Docker Compose | **Not implemented** | Running directly on host |
| `relational` atom type | **Schema only** | Defined in schema CHECK constraint but decomposer never produces it |
| Contradiction detection | **Not implemented** | Spec explicitly deferred to post-v0.1 |
| Live subscriptions | **Not implemented** | Spec explicitly deferred to post-v0.1 |
| α ≠ 1 view projections | **Not implemented** | Spec explicitly deferred to post-v0.1 |
| `survive_departure` flag | **Not implemented** | Spec mentioned as future enhancement |
| True snapshot freezing | **Not implemented** | Snapshot records atom IDs but decayed atoms still disappear from recall results |
| Manual consolidation trigger | **Spec mentions** `POST /v1/agents/{agent_id}/consolidate` | Not found as a route; consolidation runs on schedule only |

---

## Configuration Reference

All settings use the `MNEMO_` environment prefix:

| Setting | Default | Description |
|---------|---------|-------------|
| `DATABASE_URL` | `postgresql://mnemo:mnemo@localhost:5432/mnemo` | PostgreSQL connection |
| `EMBEDDING_MODEL` | `thenlper/gte-small` | Sentence transformer model |
| `EMBEDDING_DIM` | 384 | Embedding dimensionality |
| `MAX_RETRIEVAL_RESULTS` | 50 | Hard ceiling on results |
| `DEFAULT_RETRIEVAL_LIMIT` | 10 | Default max_results |
| `GRAPH_EXPANSION_MAX_DEPTH` | 3 | Max edge hops |
| `CONSOLIDATION_INTERVAL_MINUTES` | 60 | Background job frequency |
| `MIN_EFFECTIVE_CONFIDENCE` | 0.05 | Below this, atoms are deactivated |
| `DUPLICATE_SIMILARITY_THRESHOLD` | 0.90 | Above this, atoms merge |
| `DECAY_EPISODIC` | 14.0 | Episodic half-life (days) |
| `DECAY_SEMANTIC` | 90.0 | Semantic half-life (days) |
| `DECAY_PROCEDURAL` | 180.0 | Procedural half-life (days) |
| `DECAY_RELATIONAL` | 90.0 | Relational half-life (days) |
| `DEPARTURE_RETENTION_DAYS` | 30 | Days to keep departed agent data |
| `AUTH_ENABLED` | false | Enable API key auth |
| `SYNC_STORE_FOR_TESTS` | false | Synchronous /remember for tests |
