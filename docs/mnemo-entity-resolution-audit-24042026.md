# Mnemo Entity Resolution Audit (Ticket 3)

**Date:** 2026-04-24
**Phase 1 deliverable** for `docs/mnemo-iteration-23042026.md`, Ticket 3.

## What changed

`mnemo/server/llm_decomposer.py`:

1. Added an entity-resolution rule to `DECOMPOSER_PROMPT` instructing the LLM to substitute proper nouns from source context for definite-article references to generic nouns (`the test run`, `the project`, `that meeting`, `the system`, etc.). The atom must stand alone — a downstream reader should not need the original source to understand what "the test run" refers to.
2. Added an `entity_resolved: bool` field to the LLM's JSON output contract. Defaults to `true`; the LLM sets it to `false` only when the referent cannot be identified from the source context.
3. Wired the parsing loop to degrade confidence by one band when `entity_resolved is False` (subtract 0.2 before `_confidence_to_beta`). A `confidence: 0.9` with an unresolved reference lands at (4, 2) instead of (8, 1); a `confidence: 0.65` lands at (3, 2) instead of (4, 2). The degradation is small, explainable, and decay-friendly: the faster-decaying episodic half-life eats these atoms more aggressively, so recall ranking naturally deprioritises them.

No regex backstop. Unlike the T4a state-claim patterns (which are specific — `is planned`, `is currently`), the patterns for unresolved references (`the X`) are too ambiguous to match reliably: a well-formed atom `"Tom ran the test run for ABACAB on 2026-03-15"` contains the phrase `the test run` but is already entity-resolved. The LLM's explicit flag is the whole mechanism.

## Acceptance evidence

**Seeded tests** — `tests/test_llm_decomposer.py::TestEntityResolution`:

- `test_resolved_entity_high_confidence_passes_through` — `entity_resolved=true` at confidence 0.9 lands at (8, 1).
- `test_unresolved_entity_degrades_confidence_one_band` — `entity_resolved=false` at 0.9 degrades to (4, 2).
- `test_unresolved_mid_band_degrades_further` — 0.65 with unresolved falls to (3, 2).
- `test_missing_entity_resolved_field_treats_as_resolved` — backwards-compatible default.
- `test_ambiguous_case_two_projects_stay_separate` — the Review-notes ambiguous case: a paragraph with both an ABACAB and a Sampo deployment produces atoms that each identify the correct project. (Test exercises the decomposer's preservation of the LLM's entity-resolved text; it does not exercise end-to-end recall separation, which depends on embeddings.)

All assertions verified by direct Python smoke-test on inforge-ops at T3.P1 sign-off.

## Production baseline audit

Scanned `is_active = true` atoms on inforge-ops for definite-article references to generic nouns:

| Atom type | Total | the_test_run | the_project | the_system | the_deployment | the_meeting | the_feature | the_ticket | the_issue |
|---|---|---|---|---|---|---|---|---|---|
| episodic | 4,434 | 0 | 2 | 1 | 0 | 3 | 0 | 0 | 0 |
| procedural | 545 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| semantic | 4,617 | 0 | 0 | 15 | 0 | 0 | 0 | 0 | 0 |

22 atoms flagged (0.23% of active corpus). Sample review shows the pattern splits roughly 50/50 between genuine unresolved references and false positives. Examples:

**Genuinely unresolved (fix would catch these going forward):**
- *"The system has an expand_graph feature that can be toggled on/off"* — almost certainly Mnemo; atom should say so.
- *"The system includes a dedup mechanism with configurable thresholds"* — same.
- *"The system uses typed atom architecture as a core component"* — same.
- *"The project recommendation is to PARK the idea."* — which project?
- *"Deploy_request triggers the deployment step in the workflow"* — which workflow?

**False positives (not actually unresolved):**
- *"Behavioral dynamics concerns what outcomes emerge at the system level when multiple agents interact"* — "the system level" is a generic concept, not a reference.
- *"The appropriate unit of fairness evaluation in multi-agent LLM systems is the system as a whole"* — self-referential; the antecedent is in the same sentence.
- *"You cannot prove your own worth from inside the system"* — philosophical use of "system".

This is the pattern that justified dropping the regex backstop: the false-positive rate on regex-only matching would be higher than the signal. The `entity_resolved` flag lets the LLM use context to distinguish these cases.

**Narrower spot-check** — atoms containing `test run` without any of the project names (ABACAB, Sampo, Ilmarinen): 4 atoms. Low absolute, but each is a case where a future recall on "cost issues for ABACAB" could plausibly miss a relevant cost atom because the project name isn't in the atom text.

## Expected post-fix behaviour

- **New writes.** Atoms decomposed from text that references a project by name will get the project name substituted into each atom. Atoms that the LLM can't resolve against source context will land at a degraded confidence band; decay + ranking naturally deprioritise them.
- **Existing atoms.** Unchanged. Store self-heals forward — when a new atom about the same topic arrives with the resolved name, it will supersede (by recency within episodic) or simply coexist as a higher-confidence duplicate (semantic).
- **Observability.** If we want a post-deploy signal, re-run the baseline query in ~2 weeks and compare the absolute count of flagged atoms. A rising count would be concerning; a stable or slowly-falling count (since the store only grows by new-writes + consolidation) would be consistent with the fix working.

## Out of scope for Ticket 3

- **Structured entity tags.** Ticket 3 explicitly rejects these — entity lives in the text.
- **Global entity graph or normalisation layer.** Also explicitly rejected. Resolution is per-atom against per-write context, not across the store.
- **Retroactive enrichment of existing atoms.** Forward-only, same pattern as T4a.
- **Second LLM pass.** Considered and deferred in planning — the single-prompt approach matched the T4a precedent. If the production audit in ~2 weeks shows the prompt-only approach is insufficient, a second-pass LLM call remains a clean escalation.
