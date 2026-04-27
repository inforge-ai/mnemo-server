# Mnemo Lifecycle Relationship Detection — Implementation Spec

**Status:** Draft for Claude Code execution
**Author:** Tom Davis (with Claude + Hermes diagnostic input)
**Date:** April 26, 2026
**Supersedes:** earlier "Supersession Detection" draft from same date

## Context

Mnemo currently has a partial supersession pipeline:

- **Recall side (working):** `_filter_superseded()` in the retrieve path filters out atoms that have an active `supersedes` edge pointing from a newer atom to them.
- **Store side (missing):** Nothing creates `supersedes` edges automatically. The decomposer has no contradiction detection. Edges only materialize via manual API calls, which never happens in practice.

Result: `_filter_superseded()` is effectively dead code. Atoms like "Zulip integration is planned" (March) and "Zulip integration is complete" (April) coexist at similar confidence in recall results, with no resolution.

This was confirmed by Hermes's dogfood report (April 26) and code-grounded follow-up: dedup at cosine > 0.90 works, consolidation works, but lifecycle relationship detection — where two atoms are topically similar (cosine ~0.5–0.9) and their relationship needs to be characterized — does not exist.

## Design philosophy

The first version of this spec framed the problem as "supersession detection." That framing is too narrow. There are at least two distinct lifecycle relationships that share the same surface signal (high cosine similarity between topically similar atoms) but require different handling:

**State changes** are episodic transitions where the new atom replaces the old. "Zulip is planned" → "Zulip is done" is the canonical example. The old atom is now historical; the new atom is current. The right action is to retire the old atom from active recall.

**Evidential tensions** are semantic juxtapositions where the new atom and the old atom both remain true but jointly inform the reader of an unresolved discrepancy. "Newtonian gravity works" and "Mercury's perihelion precesses anomalously" is the canonical example. Neither retires the other; the tension itself is the signal. The right action is to flag the relationship without retiring either atom.

A memory system that collapses both into "latest wins" makes a category error on the second class. Mnemo's job is to maintain coherent state where coherent state exists, *and to surface tension where it doesn't*. Contradictions are signal, not noise — Mercury's perihelion drove general relativity precisely because Le Verrier didn't silently retire Newton.

This shapes the design: the LLM classifier produces a richer ontology than supersedes-or-not, and the recall path treats different relationship types differently. The agent consuming Mnemo gets clean state on actual updates and explicit tension flags on actual anomalies.

## Goal

Add automatic lifecycle relationship detection so that when a new atom relates to an existing atom in one of several well-defined ways, the appropriate edge is created. The recall path uses these edges to either filter (supersession) or surface-with-context (tension, narrowing).

## Non-goals

- No changes to dedup (cosine > 0.90). That layer works.
- No changes to consolidation. That layer works.
- No retroactive sweep over pre-existing contradiction pairs. (Optional follow-up; out of scope for v1.)
- No cross-agent relationship detection. v1 is same-agent only.

## Ontology

Four relationship types, classified by the LLM:

- **`supersedes`** — A replaces B; B becomes inactive on recall. State changes, corrections, preference updates. Episodic-ish.
- **`tension_with`** — A and B both remain active, but jointly anomalous. Both surface in recall with the relationship metadata available. Semantic-ish.
- **`narrows`** — A qualifies B without invalidating it. Both surface, with the relationship explicit.
- **`independent`** — same topic, no logical relationship. No edge, no action.

`supersedes`, `tension_with`, and `narrows` are the three edge-creating verdicts. `independent` is a no-op.

## Design

### Where it runs

Async, post-store. Same pattern as the existing decomposer pipeline — the `/remember` endpoint returns immediately with the queued store_id, and lifecycle relationship detection runs in the background alongside decomposition.

This avoids adding LLM latency to the store path. Trade-off: there is a brief window (seconds to minutes) where contradictions coexist before the edge is written. Acceptable.

### Detection band

Detection only triggers for atom pairs in the cosine similarity band **0.50 ≤ cosine < 0.90**.

- Below 0.50: different topics, no meaningful relationship to characterize.
- Above 0.90: handled by existing dedup (Bayesian merge).
- Inside the band: same topic, possibly related. This is where the LLM call earns its cost.

The 0.50 lower bound is a starting point. Calibrate empirically using logged "independent" verdict rates: if >70% of band hits are "independent," narrow the band; if <20% are "independent," widen it.

### LLM call

Use Claude Haiku 4.5 (same model as decomposer). Single-call structured output:

```
You are evaluating the relationship between a newly stored memory atom
and an existing atom about a similar topic.

EXISTING ATOM (stored {age}, type: {episodic|semantic|procedural}):
"{existing_text}"

NEW ATOM (just stored, type: {episodic|semantic|procedural}):
"{new_text}"

Classify the relationship. Respond with JSON only:
{
  "relationship": "supersedes" | "tension_with" | "narrows" | "independent",
  "confidence": 0.0-1.0,
  "reasoning": "<one sentence>"
}

Definitions:
- "supersedes": the new atom replaces the existing one. Use this for state
  changes, corrections, and preference updates where the existing atom is
  now historically accurate but no longer current. Examples:
    "X is planned" -> "X is done"
    "Tom prefers A" -> "Tom now prefers B"
    "Score is 76.1%" -> "Score was actually 82.1%; 76.1% was an earlier result"

- "tension_with": both atoms remain true and active, but together they
  identify an unresolved discrepancy or anomaly worth surfacing. Use this
  when the new atom is *evidence against* or *in tension with* the existing
  one without directly invalidating it. Examples:
    "Newtonian gravity works" + "Mercury's perihelion precesses anomalously"
    "Mnemo achieves 82.1% on LoCoMo" + "Hindsight achieves 91.4% on LongMemEval"
    "Strategy X has worked historically" + "Strategy X failed in Q4"

- "narrows": the new atom qualifies or refines the existing one without
  invalidating it. Both should remain visible together. Examples:
    "Tom uses Mattermost" -> "Tom uses Zulip for ops, Mattermost for personal"
    "Mnemo runs on Postgres" -> "Mnemo runs on Postgres 16 with pgvector"

- "independent": same topic, no logical relationship between them.
    Two unrelated facts about the same project, person, or system.

Important guardrail:
If the existing atom is a SEMANTIC claim about how the world works (rather
than an EPISODIC fact about a state, event, or measurement), strongly prefer
"tension_with" over "supersedes" unless the new atom explicitly corrects or
invalidates the existing claim with overwhelming evidence. Semantic claims
about the world are rarely retired by single new observations; they usually
accumulate evidence and shift through "tension_with" relationships.
```

**Action thresholds:**
- `"supersedes"` with confidence ≥ 0.75 creates a `supersedes` edge.
- `"tension_with"` with confidence ≥ 0.65 creates a `tension_with` edge.
- `"narrows"` with confidence ≥ 0.65 creates a `narrows` edge.
- Anything else is logged but no edge is created.

The supersession threshold is set higher than the others because false positives there are destructive (silently retiring valid memories), whereas false positives on `tension_with` or `narrows` are at worst noisy (extra edges that surface in recall context). Asymmetric thresholds for asymmetric error costs.

All thresholds are configurable via env vars (`MNEMO_SUPERSEDES_THRESHOLD`, `MNEMO_TENSION_THRESHOLD`, `MNEMO_NARROWS_THRESHOLD`) so they can be tuned post-deployment without code changes.

### Candidate selection

For each new atom, query for existing atoms with:
- Cosine similarity in [0.50, 0.90) against the new atom's embedding
- Same agent (no cross-agent detection in v1)
- Active status (not already deactivated/superseded)
- Limit: top 5 candidates

If 0 candidates, no LLM call. Most stores will be in this category.

### Edge creation

```python
create_edge(
    from_atom=new_atom.id,
    to_atom=existing_atom.id,
    edge_type=result.relationship,  # supersedes | tension_with | narrows
    confidence=result.confidence,
    metadata={
        "reasoning": result.reasoning,
        "detected_at": now(),
        "detector": "auto_lifecycle_v1",
        "cosine_at_detection": cosine,
    }
)
```

Idempotency: if an edge of any lifecycle type already exists between this atom pair, no-op. Do not create competing edges.

### Recall behavior

- **`supersedes`:** existing `_filter_superseded()` behavior. Superseded atom is hidden from recall.
- **`tension_with`:** both atoms surface in recall results. The relationship metadata is included in the result payload so the consuming agent can see "this atom is in tension with atom X" and reason about it.
- **`narrows`:** both atoms surface. Relationship metadata included so the consumer can see the qualification.
- **`independent`:** no edge, no recall change.

The recall API should expose lifecycle edges for any atom in the result set. Concretely: each returned atom carries an optional `lifecycle_edges` field listing related atoms and their relationship types. This lets agents that want to attend to tensions do so, without forcing every consumer to handle them.

### Cost gate

Initial budget assumption: ~30% of stores will have any 0.50–0.90 band candidates, ~10% will have 1+, average 1.5 candidates per triggered store. Estimated overhead: ~$0.0001 per store on top of the existing ~$0.0003 decomposer cost. Validate against actuals after first 1000 stores.

## Implementation tasks

### 1. Models

- Confirm `supersedes` edge type exists in the models module (per earlier grounding, it does — verify exact location during implementation).
- Add `tension_with` and `narrows` as new edge types in the same enum/model.
- Add migration if the edges table doesn't already store free-form metadata in a JSONB column.

### 2. Service layer

- New file: `mnemo/server/services/lifecycle_service.py`
- Function: `detect_lifecycle_relationships(atom_id: UUID, agent_id: UUID) -> list[Edge]`
- Invokes candidate query, LLM call, edge creation
- Idempotent: if any lifecycle edge already exists for the same atom pair, no-op

### 3. Async hook

- Wire into the existing async post-store pipeline (alongside the decomposer)
- Same queue infrastructure, separate task type
- Failure mode: log and skip; do not retry indefinitely. A failed lifecycle detection is not data-corrupting.
- Add a dead-letter queue for transient Haiku failures so the system degrades gracefully during Anthropic outages.
- Expose a `lifecycle_queue_depth` metric so backups are visible.

### 4. LLM client

- Reuse the existing Haiku client used by the decomposer
- Structured output with JSON mode
- Timeout: 5s
- Single retry on transient error, then enqueue to dead-letter

### 5. Eval set (forcing function — write FIRST)

New file: `mnemo/server/tests/eval/lifecycle_eval.py` (verify exact tests directory location during implementation). Each case is a sequence of stores followed by assertions on edge state and recall behavior.

**Case 1: State change (supersedes)**
```
Store: "Zulip integration is a planned future task"
Wait for async completion
Store: "Zulip integration is complete and in daily use"
Wait for async completion
Recall: "Zulip integration status"
Assert: only the second atom is returned in active recall
Assert: a supersedes edge exists from new atom to old atom
```

**Case 2: Preference change (supersedes)**
```
Store: "Tom prefers Mattermost for team communication"
Store: "Tom now prefers Zulip; Mattermost has been replaced"
Recall: "Tom communication preferences"
Assert: only the Zulip atom is returned
Assert: a supersedes edge exists
```

**Case 3: Dedup-by-rephrasing (control — should NOT trigger any lifecycle edge)**
```
Store: "test tasks consumed 89% of spend"
Store: "test tasks were cost black holes consuming 89%"
Assert: dedup merge happens (existing behavior)
Assert: no lifecycle edge of any type created
```

**Case 4: Episodic correction (supersedes)**
```
Store: "Mnemo achieves 76.1% on LoCoMo benchmark"
Store: "Actually Mnemo achieves 82.1% on LoCoMo; 76.1% was the gte-small result"
Recall: "Mnemo LoCoMo score"
Assert: only the corrected atom is returned in active recall
Assert: a supersedes edge exists
```

**Case 5: Stale-but-not-superseded (control — should NOT trigger any lifecycle edge)**
```
Store: "Tom is co-founder of Inforge LLC"
Store: "Inforge LLC was incorporated in Delaware in March 2023"
Recall: "Inforge company status"
Assert: both atoms returned
Assert: no lifecycle edge created (LLM classifies as "independent")
```

**Case 6: Narrowing**
```
Store: "Tom uses Mattermost for all communication"
Store: "Tom uses Zulip for Inforge ops; Mattermost for personal"
Recall: "Tom communication tools"
Assert: both atoms returned
Assert: a narrows edge exists from new atom to old atom
Assert: recall result includes lifecycle_edges metadata referencing the narrowing
```

**Case 7: Evidential tension on a semantic claim (tension_with, NOT supersedes)**
```
Store: "Newtonian gravity accurately predicts planetary orbits" (semantic)
Store: "Mercury's perihelion precesses by 43 arcseconds per century beyond Newtonian prediction" (semantic)
Recall: "Newtonian gravity validity"
Assert: both atoms returned in active recall
Assert: a tension_with edge exists from new atom to old atom
Assert: NO supersedes edge created
Assert: recall result includes lifecycle_edges metadata referencing the tension
```

**Case 8: Competitive benchmark tension (tension_with)**
```
Store: "Mnemo achieves 82.1% on LoCoMo multi-hop, best-in-class"
Store: "Hindsight achieves 91.4% on LongMemEval, exceeding Mnemo"
Recall: "Mnemo competitive position on memory benchmarks"
Assert: both atoms returned
Assert: a tension_with edge exists
Assert: NO supersedes edge created
```

**Case 9: Episodic supersession on a measurement (supersedes, NOT tension)**

This is the deliberate counterpoint to Case 7. The episodic/semantic guardrail must not over-fire — measurements with explicit corrections should still supersede.

```
Store: "Q3 revenue forecast is $4.2M" (episodic)
Store: "Corrected Q3 revenue forecast is $3.8M; the $4.2M number had a calculation error" (episodic)
Recall: "Q3 revenue forecast"
Assert: only the corrected atom is returned
Assert: a supersedes edge exists
Assert: NO tension_with edge created
```

The eval should run as part of CI. Each case asserts on edge state, recall behavior, and (where relevant) lifecycle metadata exposure.

### 6. Observability — prerequisite

**Current state (per code inspection, April 27):** `mnemo/server/` uses stdlib `logging` with `logger = logging.getLogger(__name__)` in 10 modules but has no central config — no `basicConfig`, `dictConfig`, handlers, or formatters. Output is whatever uvicorn/pytest defaults produce. No structured logging.

This needs to be fixed before lifecycle detection lands, both because failures are silent (a missed contradiction looks identical to no contradiction) and because every future feature will need it too.

**Task 6a: Central logging config.** Add `mnemo/server/logging_config.py` with structured JSON output to stdout (Docker captures it). Use stdlib `logging` with a JSON formatter — no new dependency required, but `python-json-logger` is acceptable if a small dep is preferred over hand-rolled. Configure on app startup in `main.py`. Include: timestamp (ISO 8601), level, logger name, message, and any `extra` fields passed by call sites. Keep the existing `getLogger(__name__)` calls as-is.

**Task 6b: Lifecycle-specific log lines.** In `lifecycle_service.py`, log every detection attempt at INFO with structured fields:

```python
logger.info(
    "lifecycle_check",
    extra={
        "event": "lifecycle_check",
        "new_atom_id": str(new_atom.id),
        "candidate_atom_id": str(existing.id),
        "agent_id": str(agent_id),
        "new_atom_type": new_atom.atom_type,  # episodic | semantic | procedural
        "existing_atom_type": existing.atom_type,
        "cosine": cosine,
        "llm_relationship": result.relationship,
        "llm_confidence": result.confidence,
        "llm_reasoning": result.reasoning,
        "edge_created": edge_created,
        "edge_type": edge_type if edge_created else None,
        "latency_ms": latency_ms,
        "haiku_input_tokens": usage.input_tokens,
        "haiku_output_tokens": usage.output_tokens,
    },
)
```

Log all four relationship verdicts, not just edge-creating ones. The "independent" verdict rate is a key calibration signal for the cosine band.

**Task 6c: Smoke test.** A test that asserts the logger emits a parseable JSON line on a known event. Cheap, catches config drift.

## Rollout

1. Implement Task 6a (central logging config) as a standalone PR. Verify JSON output in the dev environment. This is a prerequisite, not part of lifecycle detection.
2. Implement eval set as a separate PR. Confirm it fails on current codebase (no edges created, contradictions coexist).
3. Implement the lifecycle service. Run eval. Iterate until all 9 cases pass.
4. Deploy to inforge-ops first behind a feature flag (`MNEMO_LIFECYCLE_DETECTION_ENABLED=true`, default false in production), internal agents only, ~3 days observation.
5. Inspect the logged distribution of relationship verdicts. Tune thresholds and cosine band if needed.
6. Deploy to mnemo-net once confidence in cost projections, false-positive rate, and verdict distribution is established.

## Success criteria

- All 9 eval cases pass
- False-positive rate on `supersedes` (edges created where humans would say "tension_with", "narrows", or "independent") < 3% on a hand-labeled sample of 100 production cases
- False-positive rate on `tension_with` and `narrows` < 10% (higher tolerance acceptable since false positives are non-destructive)
- "Independent" verdict rate within the cosine band: 30–60% (calibration target)
- Per-store cost overhead < $0.0002 average
- Hermes's Zulip query no longer returns the "planned" atom alongside the "complete" atom in active recall
- Recall API exposes lifecycle edges for atoms that have them

## Open questions

- **Cross-agent relationship detection:** if Hermes stores "X is done" and Astraea has "X is planned", should detection fire? v1 says no (same-agent only). v2 might revisit, but raises trust questions — Hermes shouldn't be able to retire Astraea's memories unilaterally. `tension_with` may be safer to enable cross-agent before `supersedes`.
- **Confidence transfer:** when atom B supersedes atom A, should B inherit any of A's confidence/reinforcement history? v1 says no (each atom carries its own confidence). Worth measuring whether superseded atoms had high confidence and whether that information is lost.
- **Tension cluster surfacing:** when an atom has multiple `tension_with` edges, the agent recalling it should probably see this as a "this is a contested area" signal. v1 just exposes the raw edges; v1.1 might compute a tension_score per atom for ranking or display.
- **Retroactive sweep:** existing contradictions in the vault (Zulip planned/done, etc.) won't be caught by the new pipeline since it only runs on new stores. A one-time sweep over the existing band could be run after v1 stabilizes. Estimated cost: ~$50 for the full vault if every active atom triggers one Haiku call. Defer to v1.1.
- **Prompt drift discipline:** as edge cases accumulate, there will be pressure to add categories and explanations to the LLM prompt. Counter-discipline: every time a new category is proposed, prefer adding it as a low-confidence verdict the system logs but doesn't act on, until the eval data justifies promotion to a real category. The prompt should grow only when the eval data demands it.

## Product positioning note (not for implementation, for PR description)

This feature changes Mnemo's product story in a meaningful way. The pitch becomes:

> Mnemo distinguishes between *state changes* (where new information replaces old) and *evidential tensions* (where new information sits alongside old as signal). Most memory systems collapse both into "latest wins." Mnemo preserves the structure of what your agent actually knows — including the things that don't fit together cleanly.

This is differentiated from Cognee, Hindsight, and Oracle's offerings, none of which expose tension as a first-class concept. It also aligns with how research and reasoning actually work: anomalies are signal, not noise. Mercury's perihelion drove general relativity precisely because it wasn't silently retired.

## References

- Hermes dogfood report (April 26, 2026) — initial diagnosis of the contradiction problem
- Hermes code-grounded follow-up — identified that filter exists but edges aren't created
- `mnemo/server/services/atom_service.py` — existing dedup logic to mirror in structure
- `mnemo/server/services/consolidation_service.py` — async pipeline pattern to follow
- `mnemo/server/routes/memory.py` — `_filter_superseded()` is the consumer of the new edges; will need extension to expose `tension_with` and `narrows` edges in recall metadata
