# Mnemo v2 Phase 2 report: decide on write

**Spec:** `docs/phase2-spec.md`. **Design:** `docs/phase2-design.md` (G1 approved 2026-11-24 with six changes). **Branches:** mnemo-server `v2/phase2` (draft PR #30), mnemo-client `v2/phase2`.
Counts and ids only; every fixture and benchmark text is synthetic.

## Gates

| gate | date | outcome |
|---|---|---|
| G1 design note | 2026-11-24 | Approved, with six changes (design note, top). Answers to the open questions are recorded in design §12. |
| Kind bake-off | 2026-11-24 | `typesafe/jev-1.14`, fallback `liquid/d1.2-ultra`. Kind threshold 0.60 (Tom, after the re-score and the fixture runs). See `docs/phase2-kind-bakeoff.md`. |
| q2 wording | 2026-11-24 | Tom reworded the `same` criterion and added the observation guard on `same`, plus the `same_reading_two_days` fixture. |
| G2 deploy | — | Not yet. |

## Work items so far

- **011 migration.** Kind, validity, date basis, status (a trigger keeps it in step with `is_active`), `relation_decisions`, `conflicts`. Applied to the test DB only.
- **Decision client.** Every request carries exactly `provider: {"zdr": true}`; a test proves that a body without it is never sent. Two retries, then the fallback model, then fail open.
- **Matrix and guards.** A pure `decide()`. Stored rows can be replayed at any threshold, including the kind threshold: each side's top kind is recorded.
- **Decomposer prompt v3.** Adds `event_date` and `date_precision` per atom and drops `confidence`; v2 stays selectable. Confidence starts from a prior by `origin_class`.
- **Store path.** Candidates are fetched on one short connection. The model calls hold no connection (tested) and run under the budget; unfinished pairs are `pending` and a worker retries them. In enforce mode, edges, status, `valid_until`, conflicts and evidence are written in the persist transaction.
- **API.** Supersede, undo, conflicts, and `resolves` on remember. Recall and recent hide by status (legacy edges hide nothing), show `hidden_by`, and bring a conflict partner once.
- **MCP.** `mnemo_supersede`, `mnemo_undo_relation`, `mnemo_conflicts`; remember gains `resolves`; output shows relations.
- **Suites.** Server 738 passed (offline), client 142 passed.

## Fixtures (live; Haiku 5.5 decomposer, Jev decisions; 5 runs per case; test DB)

Runs 1 and 2 cover 29 cases. Run 3 covers 30: it adds `same_reading_two_days`.

| run | kind threshold | questions | passed | failed | xfail |
|---|---|---|---|---|---|
| 1 | 0.80 | q1 | 112 | 25 | 5 |
| 2 | 0.60 | q1 | 115 | 22 | 5 |

Run 2 failures: `updated_number` 9 of 10, `rescheduled_plan` 3 of 5, `calculation_vs_measurement` 5 of 5, `duplicate_independent` 5 of 5.

| 3 | 0.60 | q2 | 121 | 21 | 5 |

The 5 xfails are `validity_window` (out of scope).

**Run 3, failing cases:**

| case | failed | why |
|---|---|---|
| `updated_number` (both dedup thresholds) | 10 / 10 | The kinds are a mismatch for the matrix. The first atom ("the benchmark score is 76.1%") is classified `observation` and the correction `state`. The state → observation cell allows only `refines`, so the model's `replaces` (p 1.0, change 0.86) is downgraded. |
| `calculation_vs_measurement` | 5 / 5 | The calculation is a `state` and the measurement an `observation`. The state → observation cell does not allow `conflict`, so the model's `conflict` (p 0.54) is downgraded. The spec's fixture expects a conflict the matrix cannot produce for these kinds. |
| `duplicate_independent` | 5 / 5 | With q2 the model's `same` rose from 0.05 to 0.41–0.57, still below the 0.85 threshold, and is sometimes below `unrelated`. |
| `rescheduled_plan` | 1 / 5 | The new plan's kind was 0.59, just under 0.60, so it was `unknown`. |

**Passing 5/5 in run 3:**
- `negation_pair`, `planned_then_done` (no longer xfail), `series_two_days`, `reading_vs_baseline`;
- `different_days_unresolved`, `older_over_newer`, `observation_vs_rule`, `observation_correction`;
- `year_precision`, `same_reading_two_days`, `decision_outage`, `undo_replaces`;
- every Phase 1 fixture.

**Run 4** (after Tom's matrix change: state ↔ observation symmetric; `duplicate_independent` marked xfail until G3): **132 passed, 5 failed, 10 xfail**.
- `updated_number` and `rescheduled_plan` now pass 5/5.
- The only failure is `calculation_vs_measurement` (5/5). The matrix now allows `conflict`, but the model's `conflict` probability is 0.35–0.52, below the 0.60 threshold. That is a G3 tuning question.

## remember latency with the relation check

Real store path, in-process, test DB.
- **Models:** Haiku 5.5 decomposer and Jev decisions with `zdr: true`.
- **Embedder:** the fake embedder (memory on this host). Embedding takes about 1 ms, against about 150 ms with the real one.
- **Notes:** synthetic notes built from the kind bake-off atoms.
- **Seeding:** each configuration's scratch agent is first seeded with 10 three-sentence notes with relations off, so every candidate's kind is `unknown` and is asked on demand.
- **Measured:** five 11-sentence notes (the production p50 is 11 atoms per store) and two 29-sentence notes (production p95).

| config | remember p50 | remember p95 | 11 atoms: wall p50 / relation step p50 / pending pairs | 29 atoms: wall p50 / relation step p50 / pending pairs |
|---|---|---|---|---|
| relations off | 4.96 s | 11.95 s | 4.96 s / — / — | 11.73 s / — / — |
| shadow, k = 5 | 8.24 s | 15.27 s | 8.06 s / 2.83 s / 0 of 275 | 15.08 s / 3.19 s / 194 of 290 |
| shadow, k = 5, k = 3 over 20 atoms | 8.12 s | 14.90 s | 8.01 s / 2.96 s / 1 of 275 | 14.67 s / 3.22 s / 75 of 174 |

- **What the time is.** Remember is dominated by decomposition (about 5 s for 11 atoms and 11–12 s for 29 here). The relation check adds about 3 s.
- **11-atom stores** fit within the 3 s budget, narrowly: 1 pending pair in 550.
- **29-atom stores** exhaust the budget at either k. A pending pair means the store has completed and the worker decides the pair later. Nothing is lost or blocked.
- **Where the relation step's time goes:**
  - about 0.9–1.7 s on per-atom questions plus kinds for `unknown` candidates;
  - about 1.5–1.9 s on pair requests (about 0.45 s each at concurrency 16).
- **Why the budget runs out on large stores.** In shadow mode, kinds learned for old atoms are recorded only in `inputs`, never on the atom (design §4). So every store re-asks them, and on this freshly seeded agent every candidate was `unknown`.
- **Production p95 exceeds the 3 s budget at k = 5.** The proposal (below) is a smaller k for large stores, not a larger budget.

## Backfill 8.3 (kind for all active atoms): dry run, STOP

Read-only, 2026-11-24. Production has no migration 011 yet, so there is no kind column and every active atom counts as `unknown`.

| agent | active atoms |
|---|---|
| eaara | 2,346 |
| toms-claude-desktop | 1,988 |
| clio | 1,561 |
| tapio | 1,436 |
| nels-claude-desktop | 775 |
| illmarinen | 611 |
| 7 others | 379 |
| **total** | **9,096** |

- **Size.** The average atom text is 109 characters, against 113 in the kind bake-off. The per-atom state (CONTEXT, QUOTE, ATOM, EVENT DATE) should therefore cost about the same per request.
- **Cost estimate.** The bake-off measured $0.0000225 per Jev kind request, so 9,096 requests ≈ **$0.20** (d1.2-ultra fallback ≈ $0.09). About 6 minutes at concurrency 8.
- **Scripts** (run after the G2 deploy, with the production key):
  - `scripts/phase2/backfill_kind_plan.sql`: dry run, then `-v expected=N`; creates the undo table `repair_kind_2026_12`.
  - `backfill_kind_ask.py`: in the container; fills the undo table only.
  - `backfill_kind_apply.sql`: `-v expected=N`, one transaction.
  - `backfill_kind_undo.sql`.
- **Tested** end to end on the test DB with the fake decision model, including undo.
- **Order.** It runs after the G2 deploy and before `MNEMO_RELATION_MODE=shadow` is set (Tom: 8.3 before shadow).

## Tapio

0 stores since 2026-11-23 (all statuses), per Tom's instruction counted for the G2 package. The watch for its first 0.6.0rc1 store was stopped by the host's memory-pressure reaper on 2026-11-24. Tom said not to restart it.

## Deviations and things to know

- **mnemo-client base.** `v2/phase2` was first stacked on `v2/phase1`. After Tom merged client PR #2 (`ca17e27`), it is based on `main`.
- **Same-source duplicates are never compared.** The candidates exclude the atom's own note, so the spec's "duplicate from the same source" case is covered offline at `apply()`.
- **Different-days fixture.** Step 0 is stored as an explicit, undated atom, like a pre-Phase-2 atom. The v3 decomposer always resolves "tomorrow" against the note date, so an unresolved time can only come from old atoms.
- **Time question for candidates.** Lazy candidate classification also asks the time question when a candidate has relative-time words and no stored answer.
- **Memory.** The host has about 7.6 GB, with about 1.3 GB available during these runs. The memory-pressure reaper stopped two background jobs on 2026-11-24.
