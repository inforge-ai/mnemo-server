# Mnemo Improvement Spec
_Generated 2026-03-17. Based on dogfooding observations and cross-call edge linking diagnostic._

---

## 1. Cross-Call Edge Inference

### Problem
Edges between atoms are only created within a single `mnemo_remember` call. Atoms stored in separate calls are islands — no edges connect them even when they are clearly about the same topic. Similarity search compensates well enough at current scale, but the graph becomes noisier as memory grows and the compensation degrades. Confirmed by Nels's March 9 feedback and reproduced in a diagnostic run on March 17.

### Solution
Add a recall step inside the async store pipeline, after decomposition and before finalising storage. For each new atom, query existing memories via ANN search. Any existing atom above a similarity threshold gets an explicit edge created to the new atom.

### Scope Clarification — Edges Only, Not Merging
This change **strictly creates edges** between new and existing atoms. It does **not** merge, update, or deduplicate existing atoms when a near-duplicate is found. Merging (e.g. combining two atoms about the same claim, updating confidence, or retiring a stale atom in favour of a new one) is a materially larger change to the store pipeline and the decomposer's responsibilities. It is explicitly out of scope here. The decomposer prompt should instruct Haiku to use recalled context for edge creation only — not to suppress, rewrite, or consolidate atoms based on what already exists.

### Implementation Notes
- The ANN search uses the existing pgvector HNSW index — complexity is O(log N) per atom, not O(N²). For a store call that decomposes into K atoms, this is K ANN queries. Manageable at all foreseeable scales.
- Latency impact is on the write path only, which is already async. Not user-facing.
- Queue saturation under high concurrent agent load is a theoretical concern but not observable at current scale. Instrument and monitor; do not pre-optimise.
- Suggested similarity threshold: 0.78 (consistent with existing `min_similarity` floor for gte-small). Tunable.
- This is a **code change to the async store pipeline** plus a **prompt change** to instruct Haiku what to do with the recalled atoms. Both are required — the prompt alone cannot work because the decomposer currently has no access to existing memories during a store call.

### Scope
- Change: `mnemo-server`, async store pipeline
- No schema changes required — edges table already exists
- No MCP or client changes required

---

## 2. Decomposer Token Logging

### Problem
The Haiku decomposer runs on every `mnemo_remember` call but its token consumption is untracked. As design partners are onboarded, operator-level cost is invisible. This matters for unit economics, future billing, and understanding decomposer load as agent count scales.

### Solution
Log token usage per decomposer call to a new `decomposer_usage` PostgreSQL table. No new infrastructure — same database, natural extension of the existing schema.

### Schema

```sql
CREATE TABLE decomposer_usage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    store_id        UUID NOT NULL,           -- links to the originating remember call
    operator_id     UUID NOT NULL,
    agent_id        UUID NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    cache_creation_input_tokens  INTEGER,    -- nullable, Anthropic prompt caching
    cache_read_input_tokens      INTEGER,    -- nullable, Anthropic prompt caching
    output_tokens   INTEGER NOT NULL
);

CREATE INDEX idx_decomposer_usage_operator_created
    ON decomposer_usage (operator_id, created_at);
```

### Notes
- Log all cache token fields even if null — rate cards differ for cache creation vs cache reads vs standard input. The CFO agent needs the full picture to calculate cost correctly.
- The `(operator_id, created_at)` index is added from the start — the CFO agent's first query will be "show me this operator's usage for the last N days" and this avoids a sequential scan on what will be a high-volume table.
- No cost calculation in Mnemo — that is the CFO agent's responsibility.
- No API endpoint needed yet. The CFO agent will query this table directly when built.
- No admin console needed yet. The forcing function for an operator-facing usage API is a design partner asking "what am I consuming?" — not an internal need.

### Scope
- Change: `mnemo-server`, async store pipeline (same location as item 1)
- Schema change: new `decomposer_usage` table
- No MCP or client changes required

---

## Deferred — Not Yet

The following were raised but explicitly deferred to avoid premature complexity:

- **Atom merging / deduplication** — the cross-call edge inference deliberately does not merge near-duplicate atoms. A future pass could allow the decomposer to consolidate atoms (update confidence, retire stale versions), but this requires rethinking the decomposer's role from "writer" to "writer + editor" and is a separate design decision.
- **Admin console / usage dashboard** — deferred until a design partner requests visibility into their consumption
- **Operator-facing usage API** — same trigger as above
- **Queue depth / store latency monitoring** — instrument when queue saturation is observable, not before. The cross-call edge inference adds K ANN queries per store call; monitor whether this changes store latency in practice once implemented.

---

_Both items 1 and 2 touch the same location (async store pipeline) and should be implemented together in a single pass._
