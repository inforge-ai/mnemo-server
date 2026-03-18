# LLM Decomposer: Add Type Classification

## Status: Ready to build
## Priority: High — without this, skill export is empty when using the LLM decomposer
## Prerequisite: None (self-contained change to llm_decomposer.py)

---

## Problem

The LLM decomposer (`llm_decomposer.py`) hardcodes all atoms as `semantic`. This means:

- `export_skill` filters for `atom_type == "procedural"` and finds nothing
- Edge inference in the store pipeline can't create `evidence_for` or `motivated_by` edges (those depend on type mix)
- Arc atom `summarises` edges still work, but the graph is flatter than it should be
- The regex decomposer produces typed atoms but relies on brittle keyword matching ("always", "should", "when X do Y") — the LLM can infer intent, not just scan for markers

The comment in the current code says "the LLM focuses on content quality, not type classification." That was a reasonable first pass, but it breaks the skill export pipeline.

---

## Fix

Add `type` to the LLM output schema. Haiku classifies each atom as episodic, semantic, or procedural based on the same definitions the regex decomposer uses, but with actual comprehension.

### Change 1: Update the system prompt

Current prompt asks for: `{"text": "...", "confidence": 0.0-1.0}`

New prompt asks for: `{"text": "...", "type": "episodic|semantic|procedural", "confidence": 0.0-1.0}`

Add type definitions to the prompt:

```
Types:
- episodic: A specific experience, event, or observation tied to a moment in time.
  "I discovered that row 847 had a string in the account_id column."
- semantic: A general fact about how something works, independent of any specific event.
  "pandas.read_csv silently coerces mixed-type columns."
- procedural: A rule, practice, or instruction for future behavior.
  "Always specify dtype explicitly when using read_csv."
```

Keep the prompt tight. Haiku doesn't need long explanations — the examples carry the signal.

### Change 2: Map the type from the LLM response

In `llm_decompose()`, replace the hardcoded `atom_type="semantic"` with:

```python
atom_type = item.get("type", "semantic")
if atom_type not in ("episodic", "semantic", "procedural"):
    atom_type = "semantic"  # fallback for unexpected values
```

### Change 3: No other changes needed

- The store pipeline already handles all three types correctly (decay half-life assignment, duplicate detection scoped by type, edge inference)
- `infer_edges()` in the regex decomposer already creates edges based on type mix — those same edges get created in `atom_service.store_from_text()` regardless of which decomposer produced the atoms
- Arc atom creation is handled separately and is unaffected

---

## What This Unlocks

- Skill export works with LLM-decomposed memories (procedural atoms exist to export)
- Edge inference produces richer graphs (episodic→semantic, procedural→semantic links)
- Decay half-lives are assigned correctly (14d episodic vs 180d procedural instead of everything at 90d semantic)
- The full remember→accumulate→export_skill pipeline becomes viable

---

## Test Plan

### Unit tests (test_llm_decomposer.py):

**test_type_classification_procedural**: Input text containing a clear rule ("Always run migrations before deploying"). Assert the atom has `atom_type="procedural"`.

**test_type_classification_episodic**: Input text describing a specific experience ("I found a deadlock when running the batch job yesterday"). Assert `atom_type="episodic"`.

**test_type_classification_semantic**: Input a general fact ("PostgreSQL uses MVCC for concurrent access"). Assert `atom_type="semantic"`.

**test_mixed_input_produces_mixed_types**: Input the canonical three-sentence example from the spec. Assert at least two distinct atom types in the output.

**test_invalid_type_falls_back_to_semantic**: Mock the LLM to return an invalid type string. Assert fallback to `semantic`.

### Integration test (test_api.py):

**test_llm_decomposed_skill_export_has_procedures**: Remember a multi-sentence input containing procedural language (with LLM decomposer active). Create a view filtered to procedural atoms. Export skill. Assert `procedures` list is non-empty.
