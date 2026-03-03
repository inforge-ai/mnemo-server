# Mnemo v0.2 — Implementation Synopsis and Open Questions

**Date:** 2026-03-03
**Status:** 85/85 tests passing. Phases 1–4 complete.

This document records what was learned during implementation, highlights design tensions
discovered in the process, and identifies areas that deserve further thought before
building on top of the current foundation.

---

## What Was Learned

### The system is coherent and well-specified

The spec drove a clean implementation with very few ambiguities. The four-layer architecture
(decompose → store/embed → graph → decay) composes well. The rule-based decomposer is more
useful than it might initially seem — the episodic/semantic/procedural classification covers
a wide range of agent outputs and the confidence inference from linguistic cues (hedging,
verification language) captures meaningful signal.

### Beta distribution confidence is the right primitive

Storing confidence as Beta(α, β) rather than a single float pays off immediately. Bayesian
merging of duplicate atoms (`α_new = α₁ + α₂ − 1`) is both theoretically sound and
intuitive: two agents independently observing the same fact increases confidence in proportion
to the evidence. Exposing only `confidence_expected` and `confidence_effective` to API
consumers (hiding α and β) keeps the interface clean while retaining full internal richness.

### Snapshot isolation is more fragile than the spec implies

See "Critical Issue 1" below.

### The cluster + merge interaction is an emergent behaviour, not a bug

When the consolidation job clusters 3 identical-embedding episodic atoms and then merges
them (because their cosine similarity is 1.0, above the 0.90 merge threshold), the
generalised semantic atom ends up with only 1 edge instead of 3. This is correct: the 3
episodic atoms collapsed into 1 survivor, so 1 edge is the accurate representation of
the provenance chain. But this is **not documented anywhere** and will surprise future
maintainers. The cluster and merge steps are not fully independent — their order matters
and their interaction changes the result.

---

## Critical Issue 1: Snapshot Immunity vs. Consolidation Deactivation

**This is the issue flagged in the conversation log and deserves the most attention.**

### What the spec says

> "Snapshots freeze atom IDs into `snapshot_atoms` at creation time (immune to later decay)."

This means: if you create a snapshot at time T that captures atom A, and at time T+1 atom A
would no longer pass the min_confidence filter, A should still be accessible through the
snapshot.

### What actually happens

The `recall_shared` function in `view_service.py` includes this filter:

```sql
FROM snapshot_atoms sa
JOIN atoms a ON a.id = sa.atom_id
WHERE sa.view_id = $2
  AND a.is_active = true   -- ← THIS LINE
```

And further filters by:
```python
rows = [r for r in rows if r["confidence_effective"] >= min_confidence]
```

So when consolidation runs and deactivates an atom (either via decay or via the merge step),
the atom's `snapshot_atoms` row persists but the atom becomes invisible in shared view recall
because `is_active = false`.

**The spec's claim of "immunity to later decay" is not implemented.** The `snapshot_atoms`
table correctly retains the atom IDs (they're not removed when atoms are deactivated), but
the retrieval logic treats deactivated atoms as invisible regardless of whether they appear
in a snapshot.

### Three possible positions to take

**Option A: Accept the current behaviour, clarify the spec**
The spec's claim of immunity was intended to mean "immune to deletion" — the snapshot
ID set is stable even if source atoms are deleted (cascade handles this). Decay and
deactivation are separate concerns. Shared views degrade gracefully as the underlying
atoms age.

*Implication:* Update the spec to say "stable IDs; access decays naturally." The
snapshot remains useful but its effective content shrinks over time. This is probably
the most honest characterisation of what the system does.

**Option B: Make snapshots truly immune**
Add a `is_snapshot_pinned` column to atoms, set it `true` for any atom in at least one
`snapshot_atoms` row. The `recall_shared` path skips the `is_active` filter and
`effective_confidence` filter. The decay job should also skip pinned atoms.

*Implication:* More complex. Atoms can be both "dead" (from the owner's perspective)
and "alive" (from a grantee's perspective via a snapshot). Need to decide what happens
when the owning agent is purged.

**Option C: Add a snapshot-level TTL**
Snapshots have an `expires_at` field. When they expire, `snapshot_atoms` rows are
cleaned up (or the view is deleted). This gives the grantor explicit control over
how long a snapshot remains valid, independent of individual atom decay.

*Implication:* This is the most operationally controllable option. The spec already
has `expires_at` on `capabilities`. Extending it to views is natural.

**Recommendation:** Option A for v0.2 (acknowledge the current behaviour), Option C
for v0.3 (add view-level expiry).

---

## Critical Issue 2: Merge-Created Edges to Deactivated Atoms

When the merge step absorbs atom B into atom A, it creates an audit edge:

```sql
INSERT INTO edges (source_id, target_id, edge_type, weight)
VALUES (older_id, newer_id, 'generalises', 1.0)
```

This edge points from an **active** atom (older_id) to a **deactivated** atom (newer_id).
The `edges` table has no constraint preventing edges to inactive atoms.

The consequence: graph expansion that does not filter by `is_active` will follow this edge
to a deactivated atom. The current `graph_service.py` expansion query should be checked to
confirm it includes `AND a.is_active = true` in its recursive CTE. If it does not, a user
could traverse an edge to a "ghost" atom and receive content from a supposedly deactivated
memory.

Even if the expansion correctly filters, the `generalises` edge to the deactivated atom
exists permanently and will accumulate in the `edges` table. Over time, a heavily-merged
agent's graph will have many edges pointing to inactive atoms with no way to clean them
up (there is no consolidation step that prunes dead edges).

**Recommendation:** Add a consolidation sub-step that deletes edges where either endpoint
has `is_active = false`. Or alternatively, when merging B into A, delete the `generalises`
audit edge and instead record the merge event only in `access_log`.

---

## Important Issue 3: Consolidation Is Not Transactional Per Step

The five consolidation steps (decay, cluster, generalise, merge, purge) all run within a
single `asyncpg` connection but there is no explicit `BEGIN / COMMIT / ROLLBACK` wrapping
each step individually. If the process crashes mid-consolidation (e.g., between cluster and
merge), the DB is left in a partially-consistent state:

- Generalised atoms exist without their episodic cluster members being merged
- Or the merge is partially complete (some pairs merged, some not)

The current code is correct for a single-threaded process (asyncpg's implicit transaction
semantics), but there is no retry/recovery logic and no idempotency guarantee.

**Recommendation:** Wrap each step in an explicit `async with conn.transaction():` block.
This ensures each step is atomic. A crash mid-step will roll back only that step. Add a
`last_consolidation_at` field to the agents table or a consolidation_state table to track
what was last successfully completed, enabling recovery runs.

---

## Design Issue 4: O(N²) Cluster Self-Join at Scale

The clustering step performs a self-join on the `atoms` table:

```sql
FROM atoms a1
JOIN atoms a2
  ON a1.agent_id = a2.agent_id AND a1.id < a2.id AND ...
  AND 1 - (a1.embedding <=> a2.embedding) > 0.85
```

With N episodic atoms per agent, this generates O(N²) candidates. pgvector's IVFFlat index
helps with nearest-neighbour search but a self-join with a threshold is a fundamentally
expensive operation.

In practice: with the current per-agent scoping and domain_tag overlap filter, N is likely
small (< 200 episodic atoms per agent). But as agents accumulate memories over months, this
will become the dominant performance bottleneck in the consolidation job.

**Recommendation for scale:** Replace the self-join with a HNSW index (pgvector 0.5+) and
query for the top-K nearest neighbours per atom. Process candidates in Python and build the
union-find structure there. This shifts the work from a cartesian-product SQL query to K
well-indexed ANN lookups.

---

## Design Issue 5: The "Already Generalised" Guard Is Asymmetric

The cluster query's "skip already generalised" guard checks:

```sql
AND NOT EXISTS (
    SELECT 1 FROM edges e
    JOIN atoms src ON src.id = e.source_id
    WHERE e.target_id = a1.id
      AND e.edge_type = 'generalises'
      AND src.source_type = 'consolidation'
)
```

This correctly excludes atoms that are the *target* of a `generalises` edge from a
consolidation-created atom. But what about the **generalised atom itself**? After a
cluster is formed and a semantic atom N is created covering {A, B, C}, subsequent
episodic atoms {D, E, F} with similar embeddings will form a new cluster because they
have no `generalises` edges pointing to them yet. This creates a second semantic atom
M covering {D, E, F}.

The result: multiple generalised atoms covering overlapping semantic territory, with no
relationship between N and M. Over time this produces a fragmented knowledge graph
with no coherent high-level structure.

**Recommendation:** After cluster generalisation, check whether the new generalised
atom is similar (cosine > threshold) to any existing consolidation-created semantic
atom. If so, merge them at the semantic level using the same Bayesian confidence update
rather than creating a duplicate. This is a second-order consolidation pass (generalising
generalisations).

---

## Design Issue 6: The Decomposer Has Known Blind Spots

The rule-based sentence classifier uses regex patterns. Several known failure modes:

| Input | Expected | Actual | Reason |
|---|---|---|---|
| "I will always use parameterised queries" | procedural | episodic | "I" + future tense parsed as first-person before "always" pattern |
| "I never understood why this works" | episodic | procedural | "never" matched before first-person check |
| "You should probably consider using X" | procedural | procedural (but over-confident) | "should" matches procedural; "probably" doesn't reduce confidence enough |
| Multi-sentence with code blocks | varies | fragments at code markers | tokeniser splits on `.` inside identifiers like `torch.nn.Module` |

None of these cause test failures because the tests use carefully chosen sentences.
Real agent output will be noisier.

**Recommendation:** Add a small golden-set of real agent outputs to the decomposer tests
(not just clean examples). Consider adding a special-case tokeniser pass for code
identifiers before sentence splitting.

---

## Pending Work (Phases 5–6)

### Phase 5: Skill Files

The spec (§5) defines two skill files that describe how an AI agent should use Mnemo:
- `mnemo/skills/claude_skill.md` — Claude-specific integration guide
- `mnemo/skills/openclaw_skill.md` — OpenClaw integration guide

These are markdown documents that tell the agent what endpoints exist, when to call
`/remember` vs `/recall`, how to interpret confidence values, and how to share views.
They are not code; they are prompt engineering artifacts. The `MnemoClient` and all
REST endpoints are complete, so these files can be written now.

### Phase 6: MCP Server

`mnemo/mcp/__init__.py` exists but is empty. The MCP server should expose:
- `mnemo_remember(text, domain_tags)` — tool
- `mnemo_recall(query, min_confidence, max_results)` — tool
- `mnemo_stats()` — tool
- `mnemo_export_skill(view_id)` — tool
- `mnemo_grant(view_id, grantee_id)` / `mnemo_revoke(capability_id)` — tools

The `mcp` package is already in `pyproject.toml`. The `MnemoClient` covers all
endpoints. The MCP server would be a thin wrapper (~150 lines) that reads `MNEMO_AGENT_ID`
and `MNEMO_BASE_URL` from environment, instantiates a `MnemoClient`, and maps each MCP
tool call to the corresponding client method.

The outstanding question is where to put `agent_id`. Options:
1. **Environment variable** — simplest; one agent per MCP server process
2. **Tool parameter** — every tool call includes an `agent_id`; supports multi-agent use
3. **MCP session context** — if the MCP framework supports per-session state

Option 1 is correct for v0.1 (single-agent deployment).

---

## Summary Table

| Issue | Severity | Effort | Recommended action |
|---|---|---|---|
| Snapshot immunity vs. deactivation | High — affects correctness guarantees | Medium | Clarify spec (Option A) now; add view TTL (Option C) in v0.3 |
| Edges to deactivated atoms accumulate | Medium — ghost edges pollute graph | Low | Add dead-edge pruning to consolidation |
| Consolidation not transactional | Medium — crash leaves partial state | Medium | Wrap each step in explicit transaction |
| O(N²) cluster self-join | Low now, High at scale | High | Switch to per-atom ANN query at N > 500 |
| Multiple generalised atoms for same concept | Low — fragmented knowledge graph | Medium | Add second-order cluster pass (generalise generalisations) |
| Decomposer regex blind spots | Low for current tests, Medium in production | Medium | Add golden-set of real agent outputs to test suite |
| Skill files missing | Low — cosmetic, not functional | Low | Write markdown documents (Phase 5) |
| MCP server missing | Low — alternate access path | Low | Implement thin wrapper (Phase 6) |
