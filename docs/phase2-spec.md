# Mnemo v2 — Phase 2 spec: decide on write

**Date:** 2026-11-24
**For:** the Claude Code session on the mnemo server.
**Owner:** Tom Davis. **STOP** means report and wait for Tom.
**Prerequisite:** production on `9b5776b` or later, PR #29 merged, `MNEMO_RECALL_HIDE_SUPERSEDED=false`.
**Context:** `mnemo-v2-plan.md`, `mnemo-v2-status-2026-11-24.md`, `docs/phase1-report.md`, `docs/phase1-bakeoff.md`.

## Goal

When a new atom arrives, the store decides how it relates to what is already there. The possible outcomes are:

- it is the same as an existing atom;
- it replaces one;
- it refines one;
- it conflicts with one;
- it is unrelated to everything.

The decision uses the kind of each atom and how good its date is. A replacement hides the old atom; nothing else does. Every decision is visible to the agent and can be undone.

## What the review taught us

The old detector hid atoms it should not have. A day's reading retired a baseline. Two notes about different days were read as a state change. The fix is not a better threshold. Whether one atom can replace another depends on what kind of thing each is, and on whether their dates can be compared at all.

## Rules

1. Branch `v2/phase2` off `main` in `mnemo-server`, and a matching branch in `mnemo-client`. Work through PRs. No direct merges, and no deploy without Tom.
2. Production data changes follow the established pattern: dry run, **STOP**, guarded execute with an expected count, undo table, verify.
3. Keep atom text, source text and pair text out of the repo, logs, commits and reports. Fixtures are synthetic. Any review that needs text gets a script for Tom to run himself.
4. **Decision model calls:**
   - Every request to OpenRouter sends `zdr: true`.
   - If a request cannot be sent with it, it is not sent.
   - Calls run with no database connection held.
5. Leave every consolidation flag off.
6. **Out of scope:**
   - ranking and currency weighting, lexical search, rerank, the recall log, and decay-clock changes (Phase 3);
   - sharing (Phase 4);
   - episodes and consolidation (Phase 5).

## Concepts

### Kind (`atoms.kind`)

| Kind | What it is | Synthetic example |
| --- | --- | --- |
| `state` | A property that holds until it changes | "The upgrade is mid-way." "Resting HR baseline is 52." |
| `plan` | An intention or schedule about the future | "Physio is booked for Thursday." "We will migrate after the review." |
| `observation` | Something that happened or was measured at a time | "Ran 6 km on 2026-11-23." "HR was 55 this morning." |
| `rule` | A general fact, preference or procedure, not tied to a time | "Tom prefers metric units." "Deploys need the free-space check." |
| `unknown` | Couldn't be decided | |

`plan` is added to the three kinds from the review. Without it, the planned-then-done case has nowhere to go.

### Date precision (`atoms.date_precision`)

The precision is one of `day`, `month`, `year` or `unknown`. It applies to the atom's event date, which is `valid_from` (below).

The Phase 1 follow-up counts show that the old decomposer used placeholder dates:

- 84 active atoms sit on 1 January. That is about 12 times an even spread.
- The 1st of other months averages about 25 atoms, against about 7 for an ordinary day.

So 1 January stood for "year only", and probably the 1st of a month stood for "month only".

### Validity (`atoms.valid_from`, `atoms.valid_until`)

- **state** and **rule:** `valid_from` is the event date, or else `remembered_on`. `valid_until` is set when the atom is replaced.
- **plan:** `valid_from` is when it was made. `valid_until` is set when it is fulfilled, cancelled or rescheduled.
- **observation:** `valid_from` is the event date, and `valid_until` is null. An observation does not expire.

### Status (`atoms.status`)

The status is `active`, `superseded`, `merged` or `archived`. Only the relation decision (v2) or the explicit supersede API may set `superseded` or `merged`. Legacy edges never change status.

Recall hides `superseded` and `merged` atoms when `recall_hide_superseded` is true. Because legacy edges no longer set status, it is safe to turn hiding back on at gate G3.

### Relations

| Relation | Action |
| --- | --- |
| `same` | Link the new atom to the existing one; the new atom becomes `merged`. Add evidence to the existing atom only if the sources are independent (different `source_id` and different store). |
| `replaces` | The old atom becomes `superseded`, with `valid_until` set to the new atom's `valid_from`. |
| `refines` | Link only. This replaces `narrows`. |
| `conflict` | Keep both atoms active and open a conflict. The store never resolves a conflict. |
| `unrelated` | Nothing. |

### The kind matrix

Each cell lists the relations allowed between a new atom (row) and an existing atom (column). Anything else the model says is downgraded to `refines`, or to `unrelated` if the model's `refines` probability is below threshold.

| new ↓ / old → | state | plan | observation | rule |
| --- | --- | --- | --- | --- |
| **state** | replaces†, refines, conflict | replaces‡, refines | refines | refines, conflict |
| **plan** | refines | replaces†, refines | refines | refines |
| **observation** | replaces‡, refines, conflict | replaces‡, refines | replaces§, refines, conflict | refines, conflict |
| **rule** | refines | refines | refines | replaces†, refines, conflict |

- † Same subject and same attribute, from the same origin class.
- ‡ Only if a second, narrower question also says yes. The question: "Does the new atom report that this state has changed, or that this plan happened, was cancelled or was moved?" This is the only way an observation ends a state or a plan. For example, "the upgrade finished on 23 Nov" ends "the upgrade is mid-way". A reading of a different quantity does not end a baseline.
- § Correction only: the same event, both dates at `day` precision and equal, and the new atom corrects the old one. Two observations on different days never replace each other.

`same` is allowed in any cell where both atoms have the same kind.

**Global guards on `replaces`.** If any guard fails, the decision is recorded as `review`, which links the atoms but hides nothing.

- The new atom's `valid_from` is not earlier than the old one's, compared at the coarser of the two precisions.
- Both dates are at least `month` precision, and neither atom is flagged `time_unresolved`.
- If the two origin classes differ and the atoms disagree, the result is `conflict`, not `replaces`.
- The model's probability for the decision is at least its tuned threshold.

## Work items

### 1. Design note — **STOP, gate G1**

Write `docs/phase2-design.md` covering:

- migration 011;
- the exact text of every decision-model question and state template;
- the matrix as code;
- the latency budget;
- API and MCP signatures;
- the shadow-review scripts.

Tom will review it before any feature code is written.

### 2. Migration `011_relations.sql`

- `atoms` gains:
  - `kind` (default `unknown`);
  - `kind_p` (the model's probability);
  - `status` (default `active`, kept in step with `is_active`);
  - `valid_from`, `valid_until`;
  - `date_precision` (default `unknown`).
- `edges` gains:
  - `decided_by`: `legacy_lifecycle`, `relation_v2`, `relation_v2_backfill` or `agent`. Existing rows get `legacy_lifecycle`.
  - `retired_at`.
- `relation_decisions`: one row per pair asked. Columns:
  - `new_atom_id`, `old_atom_id`, `model`;
  - `probabilities JSONB`;
  - the raw choice, the final relation after the matrix and guards, and which guard fired;
  - `mode` (`shadow` or `enforce`), `latency_ms`, `created_at`.
- `conflicts`: `id`, `atom_a`, `atom_b`, `opened_at`, `resolved_by_atom_id`, `resolved_at`.

### 3. Decision client

- A `DecisionModel` protocol with an OpenRouter Decisions implementation. Primary model `typesafe/jev-1.14`, fallback `liquid/d1.2-ultra`.
- Read the current alpha docs before coding and record the date. Batch several questions into one request where the API allows it.
- Timeout per request, two retries on 429 and 5xx, then the fallback model, then give up.
- **Fail open:** if no decision is available, store the atoms anyway and record the relation as `pending`. Pending relations are retried in the background. A model outage must never block or lose a `remember`.
- Record model, probabilities and latency for every call.

### 4. Kind classification

- At write time, after decomposition and before relations, ask one choice question per atom: state, plan, observation or rule.
- Below the kind threshold, store the atom as `unknown`. Unknown atoms can be linked but never replaced, and never replace anything.
- **Bake-off (small):** about 150 synthetic atoms, balanced across the four kinds. Run Jev, d1.2-ultra and the decomposer model as a generative baseline. Report accuracy, per-class recall and a reliability table. **STOP** with the numbers before choosing.
- The decomposer prompt gains two fields per atom: `event_date` (ISO, or null) and `date_precision`. It still does not decide kind.

### 5. Relation decision

- **Candidates:** the k = 5 nearest active atoms of the same agent by cosine, with no cosine floor deciding anything. Keep k configurable.
- For each pair:
  - ask the relation question (`same | replaces | refines | conflict | unrelated`);
  - where the matrix needs it, ask the narrower change question;
  - apply the matrix, then the global guards.
- **Latency:** run the relation check inside `remember`, with a 3-second budget for the whole store. Whatever hasn't finished continues in the background, and the response lists those atoms under `relations_pending`. Report p50 and p95 for remember with and without the check.
- **Retire the old write-time dedup.** The cosine merge at 0.97 goes; `same` replaces it. Keep the legacy lifecycle detector running in shadow beside v2 until gate G3, writing only edges marked `legacy_lifecycle`.

### 6. Shadow mode — **STOP, gate G2 (deploy)**

Deploy with `MNEMO_RELATION_MODE=shadow`. In shadow mode, v2 writes `relation_decisions` rows only: no edges, no status changes, no conflicts.

Run for at least 7 days or 100 decided pairs, whichever comes later. Then report, counts only:

- decisions by final relation and by guard fired;
- agreement with the legacy detector on the same pairs;
- the probability distribution for each relation.

Provide `scripts/phase2/review_shadow.sql` for Tom to run. It should:

- print each pair whose final relation is `replaces`, `same` or `conflict`, and a random 20 of the rest;
- print, for each, both atoms' text, kind, dates and precision, and the decision with its probabilities;
- write Tom's verdicts to a CSV outside the repo.

Tom's 15 judged supersedes pairs from Phase 1 are evaluated the same way: v2's decision is printed next to the legacy edge. At least the two Tom confirmed wrong must not come out as `replaces`.

### 7. Thresholds and switch-on — **STOP, gate G3**

- Tune each threshold from Tom's verdicts, aiming for zero wrong `replaces` and zero wrong `same`. Report the tradeoff for each threshold.
- On approval:
  - set `MNEMO_RELATION_MODE=enforce`;
  - turn the legacy lifecycle detector off;
  - set `MNEMO_RECALL_HIDE_SUPERSEDED=true`.
- From then on, only v2 decisions and the explicit API hide anything.

### 8. Backfill — **STOP, gate G4 before each execute**

Each step is its own dry run, guarded execute and undo table, in this order:

1. **Date precision for `haiku_v1` atoms.**
   - Null `remembered_on` → `unknown`.
   - 1 January → `year`, and the 1st of any other month → `month`, unless the atom's text contains an explicit day-and-month date matching that day.
   - Everything else → `day`.
   - Report counts per agent, including the 42 repaired Clio atoms still on 1 January.
2. **Time-unresolved flag.**
   - Candidates are atoms that match the wide relative-time list and have no explicit date: 142 at last count.
   - For each, ask the model: "Does this atom refer to a time relative to a date it doesn't state?" Set `verification.time_unresolved` from the answer.
   - "now" and "currently" usually mark a state, not an unresolved time; the question should separate the two.
3. **Kind for all active atoms.** Give a cost estimate first. Store `kind` and `kind_p`.
4. **Re-decide legacy pairs.** Covers the 152 legacy supersedes edges, the 2,197 tension edges, the 3,376 narrows edges and the 430 `merge_pairs`.
   - Run v2 on each pair in shadow and write `relation_decisions` with `relation_v2_backfill`.
   - Report a confusion table: legacy type against v2's final relation.
   - Then **STOP**. Whether to apply any `replaces` or `same` from the backfill, and whether to retire legacy edges that v2 calls `unrelated`, is Tom's decision. Retiring sets `retired_at`; nothing is deleted.

### 9. API and MCP

- **`POST /agents/{id}/atoms/{atom_id}/supersede`** takes `{by_atom_id | text, reason}` and writes an `agent` edge. MCP tool: `mnemo_supersede`.
  - If `text` is given, the new atom is stored through `remember` with the old atom forced as the target.
  - Only the owning agent may supersede.
- **Undo:** `POST /edges/{id}/undo` reverses a `replaces` or `same`: the old atom becomes `active` again, and the edge is retired. MCP tool: `mnemo_undo_relation`.
- **Conflicts:** `GET /agents/{id}/conflicts?open=true`, ranked by:
  1. the lower confidence of the two sides;
  2. whether their origin classes differ;
  3. how long the conflict has been open.

  MCP tool: `mnemo_conflicts`.
  - A resolution is a new atom stored with `resolves=[atom_a, atom_b]`. It closes the conflict and supersedes neither side.
- **Recall and recent output** include `kind`, `valid_from` with its precision, `status`, and the relation that hid an atom when hidden atoms are requested. When a recalled atom is in an open conflict, its partner is returned once, marked as the conflict partner. Partners of partners are not followed.
- **`remember` response:** each atom carries `kind`, the relations decided with the target atom ids, and `relations_pending`.

### 10. Confidence

- The initial Beta pseudo-counts come from a prior table by `origin_class`, scaled by the grounding probability. Propose the table in the design note.
- Evidence is added only on `same` from an independent source. Hedging words in the text no longer set confidence.
- `replaces` and `conflict` do not change either atom's confidence.
- Grounding: run Jev's grounding question beside the deterministic check, starting from the synthetic cut-off of about 0.09 and re-tuned in shadow. Its result goes in `verification` and in the response flags. It is a flag, not a gate.

### 11. Fixtures

All synthetic, in `tests/fixtures/v2/`. Each live case runs 5 times and must pass 5 of 5.

- **Negation (state):** "X uses A", then "X no longer uses A". Expect `replaces`.
- **Updated number (state):** baseline 52, then a later baseline of 54. Expect `replaces`.
- **Planned then done:** plan, then an observation that it happened. Expect `replaces` via the change question.
- **Rescheduled plan.** Expect `replaces`.
- **Series:** runs on two different days. Neither hides the other.
- **Reading against baseline:** a dated reading of a different quantity. Expect no `replaces`.
- **Different days:** a "tomorrow" note with no date, then a dated plan. Expect no `replaces` (time unresolved).
- **Older over newer:** the new atom is dated earlier than the old one. Expect `review`, not `replaces`.
- **Observation against rule:** a single exception doesn't retire the rule.
- **Calculation against measurement** (different origin classes). Expect `conflict`; both atoms are recalled and the partner is shown.
- **Observation correction:** same event, same day, explicitly corrected. Expect `replaces`.
- **Year-precision date.** Expect no automatic `replaces`.
- **Duplicate from the same source:** `same`, no evidence added. **Duplicate from an independent source:** `same`, evidence added.
- **Model outage:** atoms stored, relations pending, no error to the caller.
- **Undo:** a `replaces` reversed, and the old atom back in recall.
- **Regression:** every Phase 1 fixture still passes.

### 12. Report

Write `docs/phase2-report.md`, recording every gate, deviation and open question. It should answer:

1. What share of writes yield each relation in enforce mode, and how often does each guard fire?
2. What is remember latency, p50 and p95, with the relation check, and how often does the 3-second budget run out?
3. How well do legacy edges and v2 agree, by type?
4. What is the kind distribution per agent, and the share of atoms left `unknown`?
5. What do the decision calls cost per 1,000 stores?

## Exit criteria

- The negation, updated-number and planned-then-done fixtures resolve to the newer atom. Series, reading-against-baseline and different-days don't hide anything.
- No atom is hidden except by a v2 decision or the supersede API, and every hide can be undone.
- Tom's two confirmed-wrong Phase 1 pairs come out as something other than `replaces`.
- Conflicts are listed, surfaced once on recall, and closed by a resolving memory.
- A decision-model outage loses nothing and blocks nothing.
- Every decision-model request carries `zdr: true`, and a test proves it.
- CI is green, with no network and no production credentials.
