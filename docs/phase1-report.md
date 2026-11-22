# Mnemo v2 Phase 1 report — trustworthy writes

**Branch:** `v2/phase1` (off `v2/phase0`), mnemo-client `v2/phase1`. **Spec:** `docs/mnemo-v2-phas1-spec.md`. **Design:** `docs/phase1-design.md`. **Bake-off:** `docs/phase1-bakeoff.md`.
**Status, 2026-11-22:** items 1–9 are built. **Stopped at G2 (deploy) and G3 (publish the client).** Production is unchanged: it still runs `v2/phase0` @ `7a0c676` with schema 009.

## Gates

| gate | date | outcome |
|---|---|---|
| G1 design note | 2026-11-22 | Approved with amendments. All are in `docs/phase1-design.md`: status auth; atom ids in MCP output; SQL-side filters; only `ungrounded_quote` lowers the band; `abbreviation_altered` and `unsupported_term` are record-only; `source_merges`; 90 s deadline; `order_by`; prompt rules for reported speech, temporary state, corrections, note-as-data and contiguous quotes; preamble line for `user_name`. Open questions: 1 keep, 2 moot, 3 no default addressee, 4 subject in `structured`, 5 Haiku 4.5, 6 name resolution server-side. |
| G3 client publish | — | **Pending.** See the questions at the end. |
| G2 deploy | — | **Pending.** The package is below. |

The Phase 0 prerequisite (24-hour consolidation check) is not yet due. The first consolidation run after the 14:31 UTC restart was all zeros (`decayed/clustered/merged/pruned/purged` = 0). The full check is due after 2026-11-23 14:31 UTC and is required before the G2 deploy.

## What was built

| item | where | notes |
|---|---|---|
| 1 Test hygiene | `tests/conftest.py`, `scripts/test/`, `.github/workflows/tests.yml` | `.env.test` only. Live keys are stripped outside `-m eval`, and building a real Anthropic client fails the test. Fake embedder and scripted fake decomposer, both refused unless the DB URL contains `test`. `tests/test_client.py` removed. The flaky stats test is now deterministic. CI runs against pgvector with no secrets. The local test DB is `mnemo-test-pg` on 127.0.0.1:5433. |
| 3 Sources | `migrations/010_sources.sql`, `services/store_service.py` | `sources`, `atoms.source_id/source_quote/verification`, `source_merges`; `store_failures.original_text` is now nullable. The source row is written in its own transaction before decomposition. |
| 4 Synchronous remember | `routes/memory.py`, `services/store_service.py`, `services/atom_service.py` | `wait` defaults to true. 502 on decomposer failure, with the source kept. `POST /stores/{id}/retry`. The status endpoint lists atoms and merges and is restricted to the owner, its operator or admin. Decompose and embed (one batch) hold no connection, then one transaction writes everything. Lifecycle detection releases its connection during model calls. Background tasks are referenced. Stuck jobs are failed at startup. Per-store timings are logged (ids only). |
| 5 Decomposer v2 | `mnemo/server/decomposers/`, `services/grounding.py` | Provider protocol (anthropic / openrouter / regex / fake). Forced tool use, explicit retries, truncation check. Splitting at 8k characters with parallel parts. 90 s deadline. No regex fallback; the server refuses `anthropic` without a key. Prompt v2. OpenRouter is refused except against `mnemo_eval` or test databases, with an explicit flag. Deterministic checks. |
| 6 Read side | `atom_service.retrieve/recent`, `graph_service`, `routes/agents.py` | `since` / `until` / `atom_types` in the SQL `WHERE` before `LIMIT`, graph expansion included. `POST /recent` with `order_by` `stored`/`event`. `GET /agents/me`. Bare agent names resolve within the caller's operator. `source_id` and `origin_class` appear in responses. |
| 7 MCP (mnemo-client) | `~/mnemo-client` `v2/phase1`, 0.6.0, **not published** | `agent_id` is optional (UUID, name or address; defaults to `/agents/me`). Every output line starts with the atom id. Remember gains speaker / addressee / channel / origin_class, plus the description text Tom specified. Recall gains since / until / atom_types and full verbosity with a 600-character cap per atom. New tools `mnemo_recent`, `mnemo_forget`, `mnemo_store_status`. 120 s remember timeout. 133 client tests. Smoke test registers 10 tools. Checked end-to-end against a local Phase 1 server. |
| 8 Harder fixtures | `tests/fixtures/v2/` | 8 new synthetic cases: no-label attribution, third-system attribution, ticker abbreviation, long note, provider failure, reported speech, temporary state, correction. 5× repeats. `read_your_writes` and `updated_number@0.90` flipped to pass. |
| 9 Bake-off | `scripts/eval/decision_bakeoff.py`, `docs/phase1-bakeoff.md` | 5 decision models plus a generative baseline, 2 tasks, 8,820 requests. Recommends `liquid/d1.2-ultra` (one model for both tasks), with `typesafe/jev-1.14` for relation if the tasks are split. |

**Tests:** 503 non-eval tests pass locally, offline (0 non-loopback connections) and in CI. CI is green on every commit except `2c4b505` and `ce07b78` (see deviations).

## G2 package (for Tom)

- **Migration 010, dry run against production:** `mnemo migrate --dry-run --database-url postgresql://mnemo_migrator@…/mnemo` → `Pending: 010_sources` only.
  - DDL: new tables, nullable columns with no default (catalog-only), one partial index on an empty column, and dropping `NOT NULL` on `store_failures` (0 rows).
  - Production has 12,898 atoms. Expected to run in well under a second with the server stopped.
- **Full suite:** 503 passed, 0 failed (non-eval). CI is green.
- **Fixture pass rates** (live, 5× per case, prompt v2), counting runs not marked xfail:
  - Haiku 4.5: **70 / 71**. `attribution_no_label` passed 4 of 5; about 92% across all probes.
  - Haiku 5.5: **71 / 71**.
  - With both models, `validity_window` is xfail 5/5 (phase 2) and `planned_then_done` passes 5/5 as a non-strict xfail.
- **`remember` latency, before and after.** 24 synthetic notes, test DB, live Anthropic, real embedder, lifecycle on, dedup 0.97:

| server | mean | p50 | p95 | split (median): model / embed / DB |
|---|---|---|---|---|
| `v2/phase0`, queued then polled to complete | 2.36 s | 2.31 s | 3.20 s | — |
| `v2/phase1`, `wait=true`, Haiku 4.5 | 2.41 s | 2.43 s | 3.25 s | 2,260 / 145 / 5 ms |
| `v2/phase1`, `wait=true`, Haiku 5.5 | 1.39 s | 1.35 s | 1.93 s | 1,199 / 152 / 4 ms |

Synchronous `remember` costs the same as the old queue-then-wait, and the caller now gets the atoms back.

**Proposed for the deploy:** the code plus migration 010. There are two choices for Tom:
- (a) keep `claude-haiku-4-5-20251001`;
- (b) set `MNEMO_DECOMPOSER_MODEL=claude-haiku-5-5`. It passes every fixture, about halves latency and costs a tenth as much.

The deploy steps are unchanged from the spec: backup, migrate, rebuild, restart, health check, synthetic remember-then-recall on a scratch agent, delete the scratch agent.

## Deviations and things to know

- **Branch base.** Off `v2/phase0`, not `main` (PR #23 is unmerged). The PR targets `v2/phase0`.
- **Context docs.** `mnemo-v2-plan.md` is not in the repo. The Phase 0 spec is `docs/mnemo-v2-spec.md`.
- **Prompt v2 changed after G1.** Both changes are recorded in the design note, with the measurements:
  - The addressee line is explicit, and "on {speaker}'s behalf" became "stored in the memory of the agent". The approved wording attributed "you" to the speaker.
  - Rule 7 again requires a date in episodic atom text. Without it `negation_pair` failed 5 of 5, because the undated pair's cosine (0.916) is above the lifecycle band.
- **Eval runs enable lifecycle detection,** as production does. In item 1 the unit suite turned it off by default, because those tests had been calling Haiku for lifecycle.
- **Schema additions not in the spec text:**
  - `sources.domain_tags`, needed by `/retry`.
  - `source_merges`, the G1 amendment.
  - `store_failures.original_text` made nullable: new rows hold the error only, and the source row holds the text.
- **`store_failures` writes are delete-then-insert,** not an upsert. The app role has no `UPDATE` on that table in production; this was found by a test.
- **`no_subject` false positives fixed after G1.** The heuristic flagged atoms starting with a lowercase agent name (`clio …`). Known names (speaker, addressee, agent, user) now count as subjects.
- **Lifecycle log line** no longer carries the model's reasoning, which paraphrases atom text (rule 3). The reasoning stays in edge metadata.
- **Commit mistake.** `2c4b505` was pushed with only file deletions and the full feature message, because a `git add` failed and the commit was chained with `;`. `5a01865` carries the code. History was not rewritten. CI is red on `2c4b505` alone and green from `5a01865`. Separately, `ce07b78` (store timings) went out with its new test failing: the test-and-commit chain piped pytest through `tail`, which hid the failure. `cdf9363` fixed it, and those chains now use `pipefail`.
- **Outside the repo:**
  - `~/.cache/mnemo-bakeoff` holds the LoCoMo-derived bake-off items and the raw model outputs, so `run` can resume and `report` can rerun. It is public benchmark text read from `mnemo_eval`, not production data.
  - The test DB container `mnemo-test-pg` holds synthetic data only.
  - No copy of production data was made in Phase 1.

## Answers for Phase 2

1. **What share of new atoms are flagged, by flag?** There are no Phase 1 atoms in production yet. On the 24 synthetic notes (latency runs, 95 atoms over both models): `ungrounded_quote` 0%, `unsupported_term` 0%, `abbreviation_altered` 9 of 95 (9.5%), `no_subject` 9 of 95 (9.5%).
   - All 9 `no_subject` flags were lowercase agent names, since fixed.
   - All 9 `abbreviation_altered` flags were false positives of one kind: the quote spans two facts ("the ARR bridge and the NRR figure"), and each atom carries one of them. The flag is record-only, so it does no harm, but it is noise.
   - Options for Tom: keep it as is; or flag only when the atom contains a different abbreviation-shaped token, or a known expansion, in place of the missing one. Production shares can be measured after deploy from `atoms.verification`.
2. **How does `remember` latency split?** With Haiku 4.5 the model call takes 94% of the time (median 2.26 s), embedding 6% (145 ms) and the database under 1% (5 ms). With Haiku 5.5 the model call is 1.20 s.
3. **Which decision model wins each task, and is one good enough for both?**
   - Grounding: four models are tied at 90–91%. `liquid/d1.2-ultra` is the cheapest and fastest of them.
   - Relation: `typesafe/jev-1.14` wins at 94.5% and is best calibrated. Haiku 4.5 reaches 95.0% at about 70× the cost.
   - One model, `liquid/d1.2-ultra` (90.0% / 93.0%), is good enough for both, unless Phase 2 leans on contradiction detection (72.5% for it; Jev 85%).
4. **How sensitive are the candidates to option order and label wording?**
   - Option order hardly matters: 0–1.5% of labels flip, except `openai/gpt-6-luna-decisions` at 6.5% on relation.
   - Rewording matters more: 2.4–4.8% flips on grounding and 5.5–10.5% on relation.
   - The generative baseline is the most wording-sensitive on grounding (17.9%).
5. **How many of the 430 `merge_pairs` have an inactive kept atom (merge chains)?** **52.** In 47 of them the kept atom was itself merged away (`deactivation_reason = 'merged'`); the other 5 kept atoms are inactive arcs. None is missing. This is a read-only aggregate on production, 2026-11-22.

## Open items

- 24-hour consolidation check (after 2026-11-23 14:31 UTC), then tick PR #23.
- **G2:** deploy approval and the decomposer model choice.
- **G3:** client publish questions (below).
- Tom hand-labels `scripts/eval/data/relation_label_check.csv` (40 rows) to check the bake-off relation labels.
- PRs: `mnemo-server` `v2/phase1` → `v2/phase0`; `mnemo-client` `v2/phase1` → `main`; and the `inforge-ai/mnemo-mcp` "superseded" README notice. None is opened yet; awaiting the go-ahead.

### G3 questions (client publish)

What I can see locally: this machine's Claude Code runs `uvx --from mnemo-ai[mcp] mnemo-mcp` with `MNEMO_DEFAULT_AGENT_ID` set, and several Hermes-managed `uvx --from mnemo-ai[mcp]` processes are running.

1. How does each agent install the client: the uvx configs here and in Hermes, any pinned versions, and the remote agents (nels-*, eaara)?
2. Which agent upgrades first?
3. 0.6.0 needs the Phase 1 server (`/agents/me`). Publish only after G2? (Proposed: yes.)
