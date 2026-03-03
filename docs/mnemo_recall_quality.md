# Mnemo — Recall Quality Improvements

## For: Claude Code / Implementation Agent
## Status: READY TO BUILD
## Priority: High — recall quality is the product
## Prerequisite: Phase 4 complete, all 85 tests passing

---

## Context

First live dogfooding session revealed that recall returns tangentially
related results alongside genuinely relevant ones. With a small memory
(14 atoms), every atom above a low similarity threshold makes the top
results. The ranking also does not account for confidence — a moderate-
confidence tangential match ranks the same as a high-confidence direct
match if their cosine similarities happen to be close.

Three changes, in order:

---

## Change 1: Add Cosine Similarity Floor

**Problem:** Recall returns the top N atoms by similarity regardless of
how similar they actually are. With few atoms, this means everything
comes back.

**Fix:** Add a min_similarity parameter to the recall endpoint and the
retrieval query. Atoms below this floor are excluded from results.

In server/models.py, update RetrieveRequest — add:
    min_similarity: float = 0.3

In the retrieval SQL query in server/services/atom_service.py, add:
    AND 1 - (embedding <=> $query_embedding) >= $min_similarity

In client/mnemo_client.py, add min_similarity parameter to recall().

In mnemo/mcp/mcp_server.py recall tool, pass min_similarity=0.3.

Default 0.3 is conservative. Can tune later with production data.

**New tests:**

test_recall_respects_min_similarity:
  Store one atom about pandas, one about sourdough bread.
  Recall "pandas CSV loading" with min_similarity=0.4.
  Assert pandas atom appears, sourdough does not.

test_recall_returns_empty_when_nothing_relevant:
  Store sourdough atom only.
  Recall "quantum chromodynamics" with min_similarity=0.4.
  Assert empty results.

---

## Change 2: Composite Ranking Score

**Problem:** Results ranked by cosine similarity alone. Confidence is
ignored in ranking.

**Fix:** Replace pure similarity ranking with composite score:

    score = similarity * (0.7 + 0.3 * effective_confidence)

Similarity is still dominant. Confidence is a tiebreaker (multiplier
ranges from 0.7 to 1.0 as confidence goes from 0 to 1).

In the retrieval SQL:
    Add computed column: score = similarity * (0.7 + 0.3 * eff_conf)
    Change ORDER BY to: ORDER BY score DESC

Add relevance_score field to AtomResponse model.

Update MCP recall tool to display score in output.

**New test:**

test_recall_ranks_by_composite_score:
  Store "I confirmed that pandas read_csv definitely coerces types."
  Store "I think maybe pandas read_csv might have type issues."
  Recall "pandas read_csv types".
  Assert the confirmed/definite version ranks first (higher confidence
  should win when similarity is nearly equal).

---

## Change 3: Similarity Floor for Graph Expansion

**Problem:** Graph expansion follows edges regardless of query relevance.
Expanded atoms may be topically unrelated to the query.

**Fix:** After graph expansion, compute similarity of each expanded atom
to the original query embedding. Filter out expanded atoms below
min_similarity * 0.6 (lower bar than primary results). Sort remaining
expanded atoms by the same composite score.

The 0.6 multiplier means if primary floor is 0.3, expanded atoms need
at least 0.18 similarity. Deliberately permissive — expansion should
surface context you would not find by direct search, but still in the
neighbourhood of the query.

**New test:**

test_expanded_atoms_are_relevant_to_query:
  Store cooking memories and Python memories (separate domains).
  Recall "CSV parsing bugs" with expand_graph=True.
  Assert no cooking-related atoms in expanded results.

---

## Build Order

1. Change 1: Similarity floor (~30 min)
2. Change 2: Composite ranking (~30 min)
3. Change 3: Expansion filtering (~30 min)
4. Full regression: pytest tests/ -v

Total estimated: ~90 minutes

---

## How to Verify

After implementing, re-run dogfooding test:
1. Recall "payment model" — only payment-related atoms
2. Recall "how does Tom prefer feedback" — preferences first
3. Recall "quantum chromodynamics" — empty results
4. Relevance scores displayed in recall output
