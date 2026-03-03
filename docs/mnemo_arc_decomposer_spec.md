# Mnemo - Length-Aware Decomposer with Arc Atoms

## For: Claude Code / Implementation Agent
## Status: READY TO BUILD
## Priority: Medium - enhances memory richness
## Prerequisite: Recall quality improvements implemented

---

## Context

During live dogfooding, we discovered that the decomposer loses structural information when processing multi-sentence inputs. A three-sentence input about a design decision gets decomposed into three independent atoms with no record of how they relate as a sequence. The causal arc is destroyed by sentence-level decomposition.

The fix is not narrative detection. It is making the decomposer length-aware. Longer inputs get decomposed into individual atoms AND an additional episodic arc atom that preserves the full trajectory.

The spectrum: factoid, fact, vignette, arc, story, narrative. The decomposer handles this as a continuum based on input length, not as discrete categories.

---

## Design Principle

The agent calls /remember with text of any length. The decomposer always produces individual typed atoms (current behaviour). For inputs above a length threshold, it ALSO produces an arc atom that preserves the holistic structure. No new endpoint. No new tool. Same interface, richer internal representation.

---

## Change 1: Sentence Count Thresholds

Define three tiers based on sentence count after splitting:

ARC_THRESHOLD_MEDIUM = 3 (3-6 sentences: create full-text arc atom)
ARC_THRESHOLD_LONG = 7 (7+ sentences: create compressed arc summary)

In server/decomposer.py, after existing decomposition logic, add arc creation:

1. Count sentences after splitting
2. If count >= ARC_THRESHOLD_LONG: create compressed arc + link to atoms
3. Elif count >= ARC_THRESHOLD_MEDIUM: create full-text arc + link to atoms
4. Append arc atom and arc edges to the result

---

## Change 2: Full-Text Arc Atom (Medium Inputs, 3-6 Sentences)

For medium inputs, create an episodic atom containing the complete original text unmodified.

Properties:
- atom_type: always "episodic"
- text_content: complete original input text, unmodified
- source_type: "arc"
- confidence: moderate Beta(4, 2) - it is a recollection
- domain_tags: inherited from the /remember call

---

## Change 3: Compressed Arc Atom (Long Inputs, 7+ Sentences)

For long inputs, full text produces a diluted embedding. Instead, compress to 2-3 sentences capturing: starting state, key transition, outcome.

v0.2 heuristic compression (no LLM):
- Take the first sentence (starting state)
- Take the longest sentence (likely the most information-dense)
- Take the last sentence (outcome/conclusion)
- Deduplicate if any are the same sentence
- Join with spaces

Same properties as full-text arc but text_content is the compressed version.

The first/longest/last heuristic works because people naturally state context first, elaborate in the middle, and conclude with the outcome. This is a placeholder for LLM-based compression in v0.3.

---

## Change 4: The "summarises" Edge Type

Add "summarises" to VALID_EDGE_TYPES alongside evidence_for, motivated_by, generalises, supersedes.

Arc atoms link to decomposed atoms via summarises edges:
- Direction: arc_atom --summarises--> decomposed_atom
- Weight: 1.0
- One edge per decomposed atom

These edges serve two purposes:
1. Forward: recalling the arc surfaces the individual facts it contains
2. Reverse: recalling a specific fact surfaces the arc that contextualises it

---

## Change 5: Source Type "arc"

Add "arc" to allowed source_type values. This distinguishes arc atoms from regular episodic atoms in queries and stats.

Update stats endpoint:
- arc_atoms count reported separately (subset of episodic)

---

## Change 6: Graph Expansion Handles summarises Bidirectionally

Verify the recursive CTE in graph_service.py follows edges in both directions. If it only follows forward (source to target), add reverse traversal (target to source). This is needed so that:
- Recalling the arc expands to its component atoms
- Recalling a component atom expands to its parent arc

---

## Existing Test Updates

test_spec_example_full (test_decomposer.py):
  Current: 3-sentence input produces 3 atoms + 2 edges.
  After: 3 decomposed atoms + 1 arc atom + 2 original edges + 3 summarises edges = 4 atoms + 5 edges.
  Update assertions to expect the arc atom and summarises edges.

---

## New Tests

### test_decomposer.py:

test_short_input_no_arc:
  Input: "pandas coerces mixed types." (1 sentence)
  Assert: no atom with source_type="arc"

test_two_sentence_no_arc:
  Input: "I found a bug. It was in the CSV parser." (2 sentences)
  Assert: no atom with source_type="arc"

test_medium_input_creates_arc:
  Input: 3 sentences about a debugging session.
  "I was processing client data when the pipeline crashed. The error was a silent type coercion in pandas read_csv. I fixed it by specifying dtype explicitly."
  Assert: decomposed atoms exist (episodic + semantic/procedural)
  Assert: exactly 1 atom with source_type="arc"
  Assert: arc atom type is "episodic"
  Assert: arc atom text_content equals full input text
  Assert: summarises edges exist from arc to each decomposed atom

test_long_input_creates_compressed_arc:
  Input: 8+ sentences about a multi-step process
  Assert: exactly 1 atom with source_type="arc"
  Assert: arc atom text_content is SHORTER than original input
  Assert: arc atom text contains first sentence of input
  Assert: arc atom text contains last sentence of input
  Assert: summarises edges exist

test_arc_confidence_is_moderate:
  Input: 4-sentence medium input
  Assert: arc atom has alpha=4.0, beta=2.0

test_arc_inherits_domain_tags:
  Input: 4 sentences with domain_tags=["python", "debugging"]
  Assert: arc atom has domain_tags=["python", "debugging"]

test_summarises_edge_type_valid:
  Assert: "summarises" is in VALID_EDGE_TYPES

test_compression_deduplicates:
  Input: 8 sentences where first and longest happen to be the same
  Assert: compressed arc does not repeat the sentence

### test_api.py:

test_remember_medium_creates_arc_atom:
  POST /remember with 4-sentence text
  Assert: response atoms_created includes the arc atom
  Assert: response edges_created includes summarises edges

test_recall_finds_arc_by_theme:
  Store a 4-sentence arc about "debugging pandas CSV issues"
  Recall "pandas debugging experience"
  Assert: arc atom appears in results

test_recall_expands_from_arc_to_atoms:
  Store a 4-sentence arc
  Recall with query matching the arc
  Assert: expanded_atoms includes individual decomposed atoms

test_recall_expands_from_atom_to_arc:
  Store a 4-sentence arc about debugging
  Recall with query matching one specific decomposed atom
  Assert: expanded_atoms includes the arc atom

---

## Build Order

1. Add "summarises" to VALID_EDGE_TYPES (~5 min)
2. Add "arc" source_type validation (~5 min)
3. Implement _create_full_arc() (~15 min)
4. Implement _create_compressed_arc() (~20 min)
5. Implement _link_arc_to_atoms() (~10 min)
6. Add arc creation logic to decompose() (~15 min)
7. Verify graph expansion bidirectional traversal (~15 min)
8. Update stats endpoint for arc_atoms count (~10 min)
9. Update test_spec_example_full for new counts (~10 min)
10. Add all new decomposer tests (~30 min)
11. Add API integration tests (~30 min)
12. Full regression: pytest tests/ -v

Total estimated: ~3 hours

---

## What NOT To Build

- No LLM-based compression. first/longest/last heuristic is v0.2. LLM compression is v0.3.
- No new MCP tool. Agent calls remember(). Arc created server-side.
- No new API endpoint. /remember handles everything.
- No narrative detection NLP. Threshold is sentence count. Simple, deterministic, testable.

---

## How to Verify

After implementing, test with the MCP:

1. Remember a short fact: "pandas coerces types." Check stats - no arc atom created.

2. Remember a medium arc (4 sentences): "We started with an over-engineered API with explicit atom types and Beta parameters. Tom pushed back on the cognitive burden for agents. We stripped it down to /remember and /recall. The simpler API turned out to be better because it made the server responsible for encoding quality." Check stats - should see 1 arc atom.

3. Recall "API design decisions" - should get the arc atom plus individual atoms.

4. Recall "who is responsible for encoding quality" - should get the specific semantic atom, with the arc in expanded results providing context.
