# Mnemo Supersession Detection — Implementation Spec

**Status:** Draft for Claude Code execution
**Author:** Tom Davis (with Claude + Hermes diagnostic input)
**Date:** April 26, 2026

## Context

Mnemo currently has a partial supersession pipeline:

- **Recall side (working):** `_filter_superseded()` in the retrieve path filters out atoms that have an active `supersedes` edge pointing from a newer atom to them. The edge type exists in `models.py` (line 143).
- **Store side (missing):** Nothing creates `supersedes` edges automatically. The decomposer has no contradiction detection. Edges only materialize via manual API calls, which never happens in practice.

Result: `_filter_superseded()` is effectively dead code. Atoms like "Zulip integration is planned" (March) and "Zulip integration is complete" (April) coexist at similar confidence in recall results, with no resolution.

This was confirmed by Hermes's dogfood report (April 26) and code-grounded follow-up: dedup at cosine > 0.90 works, consolidation works, but contradiction detection — where two atoms are topically similar (cosine ~0.5–0.9) but logically inconsistent — does not exist.

## Goal

Add automatic supersession detection so that when a new atom updates the state of an existing atom, a `supersedes` edge is created and the recall-side filter does its job.

## Non-goals

- No changes to the recall path. `_filter_superseded()` is correct as-is.
- No changes to dedup (cosine > 0.90). That layer works.
- No changes to consolidation. That layer works.
- No retroactive supersession of pre-existing contradiction pairs. (Optional follow-up; out of scope for v1.)

## Design

### Where it runs

Async, post-store. Same pattern as the existing decomposer pipeline — the `/remember` endpoint returns immediately with the queued store_id, and supersession detection runs in the background alongside decomposition.

This avoids adding LLM latency to the store path. Trade-off: there is a brief window (seconds to minutes) where contradictions coexist before the supersession edge is written. Acceptable.

### Detection band

Supersession detection only triggers for atom pairs in the cosine similarity band **0.50 ≤ cosine < 0.90**.

- Below 0.50: different topics, no contradiction possible.
- Above 0.90: handled by existing dedup (Bayesian merge).
- Inside the band: same topic, possibly contradictory. This is where the LLM call earns its cost.

### LLM call

Use Claude Haiku 4.5 (same model as decomposer). Single-call structured output:

```
You are evaluating whether a new memory supersedes an existing memory.

EXISTING ATOM (stored {age}):
"{existing_text}"

NEW ATOM (just stored):
"{new_text}"

Do these atoms describe the same fact/state, where the new atom updates,
corrects, or replaces the existing one?

Respond with JSON only:
{
  "relationship": "supersedes" | "reinforces" | "narrows" | "independent",
  "confidence": 0.0-1.0,
  "reasoning": "<one sentence>"
}

Definitions:
- "supersedes": new atom replaces existing (e.g., "X is planned" -> "X is done";
  "Tom prefers A" -> "Tom now prefers B"; "X is true" -> "X was wrong")
- "reinforces": new atom restates or confirms existing (handled by dedup;
  should be rare in this band)
- "narrows": new atom adds qualification without invalidating existing
  (e.g., "Tom uses Mattermost" -> "Tom uses Zulip for ops, Mattermost for personal")
- "independent": same topic, not in conflict (e.g., two unrelated facts about
  the same project)
```

Only `"supersedes"` with `confidence >= 0.7` creates an edge. Everything else is a no-op.

### Candidate selection

For each new atom, query for existing atoms with:
- Cosine similarity in [0.50, 0.90) against the new atom's embedding
- Same agent (no cross-agent supersession in v1)
- Active status (not already deactivated/superseded)
- Limit: top 5 candidates

If 0 candidates, no LLM call. Most stores will be in this category.

### Edge creation

If LLM returns `"supersedes"` with confidence >= 0.7:

```python
create_edge(
    from_atom=new_atom.id,
    to_atom=existing_atom.id,
    edge_type="supersedes",
    confidence=llm_confidence,
    metadata={
        "reasoning": llm_reasoning,
        "detected_at": now(),
        "detector": "auto_supersession_v1",
    }
)
```

The recall-side filter already handles the rest.

### Cost gate

Track per-store LLM cost in the existing cost_tracker (Lloyd's domain). Initial budget assumption: ~30% of stores will have any 0.50–0.90 band candidates, ~10% will have 1+, average 1.5 candidates per triggered store. Estimated overhead: ~$0.0001 per store on top of the existing ~$0.0003 decomposer cost. Validate against actuals after first 1000 stores.

## Implementation tasks

### 1. Models
- Confirm `supersedes` edge type exists in the models module (per earlier grounding, it does — verify exact location during implementation). No changes needed.
- Add migration if the edges table doesn't already store free-form metadata in a JSONB column.

### 2. Service layer
- New file: `mnemo/server/services/supersession_service.py`
- Function: `detect_supersession(atom_id: UUID, agent_id: UUID) -> list[Edge]`
- Invokes candidate query, LLM call, edge creation
- Idempotent: if edge already exists for the same atom pair, no-op

### 3. Async hook
- Wire into the existing async post-store pipeline (alongside the decomposer)
- Same queue infrastructure, separate task type
- Failure mode: log and skip; do not retry indefinitely. A failed supersession detection is not data-corrupting.

### 4. LLM client
- Reuse the existing Haiku client used by the decomposer
- Structured output with JSON mode
- Timeout: 5s
- Single retry on transient error, then skip

### 5. Eval set (forcing function — write FIRST)

New file: `mnemo/server/tests/eval/supersession_eval.py` (verify exact tests directory location during implementation). Six canonical cases, each a sequence of stores followed by an assertion on recall results.

**Case 1: Planned -> Done**
```
Store: "Zulip integration is a planned future task"
Wait for async completion
Store: "Zulip integration is complete and in daily use"
Wait for async completion
Recall: "Zulip integration status"
Assert: only the second atom is returned
```

**Case 2: Preference change**
```
Store: "Tom prefers Mattermost for team communication"
Store: "Tom now prefers Zulip; Mattermost has been replaced"
Recall: "Tom communication preferences"
Assert: only the Zulip atom is returned
```

**Case 3: Dedup-by-rephrasing (control — should NOT trigger supersession)**
```
Store: "test tasks consumed 89% of spend"
Store: "test tasks were cost black holes consuming 89%"
Assert: dedup merge happens (existing behavior); no supersedes edge created
```

**Case 4: Correction**
```
Store: "Mnemo achieves 76.1% on LoCoMo benchmark"
Store: "Actually Mnemo achieves 82.1% on LoCoMo; 76.1% was the gte-small result"
Recall: "Mnemo LoCoMo score"
Assert: only the corrected atom is returned
```

**Case 5: Stale-but-not-superseded (control — should NOT trigger supersession)**
```
Store: "Tom is co-founder of Inforge LLC"
Store: "Inforge LLC was incorporated in Delaware in March 2023"
Recall: "Inforge company status"
Assert: both atoms returned; no supersedes edge created
```

**Case 6: Partial supersession (narrows, not supersedes)**
```
Store: "Tom uses Mattermost for all communication"
Store: "Tom uses Zulip for Inforge ops; Mattermost for personal"
Recall: "Tom communication tools"
Assert: both atoms returned; LLM should classify as "narrows" not "supersedes"
```

The eval should run as part of CI. Each case asserts on both edge state (was a `supersedes` edge created?) and recall behavior (does the filter produce the expected result set?).

### 6. Observability — prerequisite

**Current state (per code inspection, April 27):** `mnemo/server/` uses stdlib `logging` with `logger = logging.getLogger(__name__)` in 10 modules but has no central config — no `basicConfig`, `dictConfig`, handlers, or formatters. Output is whatever uvicorn/pytest defaults produce. No structured logging.

This needs to be fixed before supersession lands, both because supersession failures are silent (a missed contradiction looks identical to no contradiction) and because every future feature will need it too.

**Task 6a: Central logging config.** Add `mnemo/server/logging_config.py` with structured JSON output to stdout (Docker captures it). Use stdlib `logging` with a JSON formatter — no new dependency required, but `python-json-logger` is acceptable if a small dep is preferred over hand-rolled. Configure on app startup in `main.py`. Include: timestamp (ISO 8601), level, logger name, message, and any `extra` fields passed by call sites. Keep the existing `getLogger(__name__)` calls as-is.

**Task 6b: Supersession-specific log lines.** In `supersession_service.py`, log every detection attempt at INFO with structured fields:

```python
logger.info(
    "supersession_check",
    extra={
        "event": "supersession_check",
        "new_atom_id": str(new_atom.id),
        "candidate_atom_id": str(existing.id),
        "agent_id": str(agent_id),
        "cosine": cosine,
        "llm_relationship": result.relationship,
        "llm_confidence": result.confidence,
        "edge_created": edge_created,
        "latency_ms": latency_ms,
        "haiku_input_tokens": usage.input_tokens,
        "haiku_output_tokens": usage.output_tokens,
    },
)
```

This is sufficient for v1. Cost aggregation can be done later by parsing logs, or by a small periodic job that writes daily totals to a `cost_events` table — out of scope for this spec. Lloyd integration is a separate, downstream concern; mention it in the PR description but do not wire it in v1.

**Task 6c: Smoke test.** A test that asserts the logger emits a parseable JSON line on a known event. Cheap, catches config drift.

## Rollout

1. Implement Task 6a (central logging config) as a standalone PR. Verify JSON output in the dev environment. This is a prerequisite, not part of supersession.
2. Implement eval set as a separate PR. Confirm it fails on current codebase (no edges created, contradictions coexist).
3. Implement the supersession service. Run eval. Iterate until all 6 cases pass.
4. Deploy to inforge-ops first (internal agents only, ~2 days observation).
5. Deploy to mnemo-net once confidence in cost projections and false-positive rate is established.

## Success criteria

- All 6 eval cases pass
- False-positive rate (supersedes edges created where humans would say "narrows" or "independent") < 5% on a hand-labeled sample of 100 production cases
- Per-store cost overhead < $0.0002 average
- Hermes's Zulip query no longer returns the "planned" atom alongside the "complete" atom

## Open questions

- **Cross-agent supersession:** if Hermes stores "X is done" and Astraea has "X is planned", should supersession fire? v1 says no (same-agent only). v2 might revisit, but raises trust questions — Hermes shouldn't be able to retire Astraea's memories unilaterally.
- **Confidence transfer:** when atom B supersedes atom A, should B inherit any of A's confidence/reinforcement history? v1 says no (each atom carries its own confidence). Worth measuring whether superseded atoms had high confidence and whether that information is lost.
- **Retroactive supersession sweep:** existing contradictions in the vault (Zulip planned/done, etc.) won't be caught by the new pipeline since it only runs on new stores. A one-time sweep over the existing band could be run after v1 stabilizes. Estimated cost: ~$50 for the full vault if every active atom triggers one Haiku call. Defer to v1.1.

## References

- Hermes dogfood report (April 26, 2026) — initial diagnosis of the contradiction problem
- Hermes code-grounded follow-up — identified that filter exists but edges aren't created
- `mnemo/server/services/atom_service.py` — existing dedup logic to mirror in structure
- `mnemo/server/services/consolidation_service.py` — async pipeline pattern to follow
- `mnemo/server/routes/memory.py` — `_filter_superseded()` is the consumer of the new edges
