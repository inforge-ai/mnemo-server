# Mnemo v2 Phase 1 report — trustworthy writes

**Branch:** `v2/phase1` (off `v2/phase0`), mnemo-client `v2/phase1`. **Spec:** `docs/mnemo-v2-phas1-spec.md`. **Design:** `docs/phase1-design.md`. **Bake-off:** `docs/phase1-bakeoff.md`.
**Status, 2026-11-23:** G2 deployed. Production runs `v2/phase1` @ `e3eec95` (`0.2.0+e3eec95`), schema `010_sources`, decomposer `anthropic claude-haiku-5-5`, since 21:05 UTC. **G3 (client 0.6.0rc1) waits** for a real store from an existing 0.5.0 client to complete on the new server.

## Gates

| gate | date | outcome |
|---|---|---|
| G1 design note | 2026-11-22 | Approved with amendments. All are in `docs/phase1-design.md`: status auth; atom ids in MCP output; SQL-side filters; only `ungrounded_quote` lowers the band; `abbreviation_altered` and `unsupported_term` are record-only; `source_merges`; 90 s deadline; `order_by`; prompt rules for reported speech, temporary state, corrections, note-as-data and contiguous quotes; preamble line for `user_name`. Open questions: 1 keep, 2 moot, 3 no default addressee, 4 subject in `structured`, 5 Haiku 4.5, 6 name resolution server-side. |
| G3 client publish | — | **Pending.** See the questions at the end. |
| G2 deploy | 2026-11-22 approved; 2026-11-23 deployed | Approved after the 24-hour check, with `claude-haiku-5-5`. Deployed 2026-11-23; record below. |

The Phase 0 prerequisite, the 24-hour consolidation check, **passed** on 2026-11-23 at 20:47 UTC: 30 runs, 0 with a non-zero count, 0 failures (`docs/phase0-report-2026-11.md`, PR #23 ticked).

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
| 9 Bake-off | `scripts/eval/decision_bakeoff.py`, `docs/phase1-bakeoff.md` | Round 2: 5 decision models, 2 generative baselines, 12,390 requests. Phase 2 choice: Jev for both tasks, Liquid as fallback; the differences are not significant. The round 1 Haiku 4.5 negation result was a harness bug, now fixed. Grounding ranking (AUC) is a tie across most models. With tuned thresholds, Jev reaches 89.5% and Liquid 88.4%. Relation, scored on Tom's 47 hand labels, shows no significant differences. Data-policy screen: Jev, Liquid and Microsoft are eligible (ZDR, and `provider: {"zdr": true}` is enforced); Gemini and Luna are ineligible. No local open-weight model (closed). |

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
3. **Which decision model wins each task, and is one good enough for both?** **Tom's choice (2026-11-22): `typesafe/jev-1.14` for both tasks, `liquid/d1.2-ultra` as fallback.** The differences are not statistically significant.
   - **Grounding:** a statistical tie on ranking. ROC AUC is 0.954–0.966 for all but `microsoft/microsoft-decision-1.1` (0.913). At a 0.5 threshold `openai/gpt-6-luna-decisions` leads (89.7%), but it is not significantly ahead of Gemini, Liquid or Haiku 4.5. The differences that are significant come from threshold placement, not ranking.
   - **Relation, on Tom's hand labels:** no significant differences (91.0–94.0%; every McNemar p ≥ 0.14).
   - **Eligible for mnemo data (ZDR):** Jev, Liquid, Microsoft. Gemini and Luna are not.
   - **One model for both:** feasible among the eligible three. Jev with a tuned threshold (89.5% on grounding, 93.0% on relation, best contradiction recall) or Liquid (88.4% / 93.0%, cheapest).
4. **How sensitive are the candidates to option order and label wording?**
   - Option order hardly matters: 0–1.5% of labels flip, except `openai/gpt-6-luna-decisions` at 6.5% on relation.
   - Rewording matters more: 1.3–6.7% flips on grounding and 4.0–10.5% on relation.
5. **How many of the 430 `merge_pairs` have an inactive kept atom (merge chains)?** **52.** In 47 of them the kept atom was itself merged away (`deactivation_reason = 'merged'`); the other 5 kept atoms are inactive arcs. None is missing. This is a read-only aggregate on production, 2026-11-22.

## G2 deploy record (2026-11-23)

Runbook `scripts/phase1/README.md`. Counts and ids only.

| UTC | step | result |
|---|---|---|
| 20:47 | 24-hour check | passed (above) |
| ~20:50 | PR #23 → `main` | merged, merge commit `2217bb0`; PR #24 retargeted to `main` |
| 20:5x | preconditions | `mnemo migrate --dry-run`: `Pending: 010_sources` only; checkout `e3eec95`, clean apart from Tom's local files; 13,061 atoms |
| 20:56:52–57 | backup | `~/backups/mnemo-prod-2026-11-23.dump`, 40,282,836 B, sha256 `3ffcdab2bc8df2b0bc17f533dd216e5d5db2f11ff8956ffed0563aa606119b35`, 154 TOC entries. Row counts in the dump = live (atoms 13,061, agents 14, edges 26,224, store_jobs 727). Phase 0 backup kept: `~/backups/mnemo-prod-2026-11-21.dump`, 139,474,330 B, sha256 `46a93f4433d4ccd873db0c491a21add89c78408dd4b0a059bf696ef7531ae7ac`. Both mode 600. |
| 20:57:18 | stop, migrate | `Applied: 010_sources`. Tables `sources` and `source_merges` exist; `atoms` has `source_id`, `source_quote` and `verification`; `mnemo` has INSERT and SELECT on `sources` and INSERT on `source_merges`; 13,061 atoms. |
| 20:57:19 | old container restarted | **Deviation:** the old server (`7a0c676`) was restarted on schema 010 while Tom reviewed `user_name`, so the server was down for ~15 s instead of for the review. Safe because 010 only adds tables, columns that may be NULL, and one dropped NOT NULL. Old code has no `SELECT *` and no startup schema check. Health was `ok`. No stores arrived before the rebuild. |
| ~20:58 | `user_name` dry run | 10 CSV rows; each name 1 active match and 1 overall; none set; `would_update` 10 |
| ~20:59 | `user_name` exec (Tom's go) | `updated 10 (expected 10)`, committed; 10 agents with a non-empty `user_name` |
| 20:59 | `.env` | appended `MNEMO_DECOMPOSER_MODEL=claude-haiku-5-5` (previous copy `~/backups/env-2026-11-23.bak`, mode 600) |
| 20:59–21:04 | build | image `420f4d794b09`, `BUILD_COMMIT=e3eec95`. **The disk filled** during the build (the image is 23.9 GB; 17 GB was free). The image finished; only compose's metadata file in `/tmp` failed. Free space recovered to 24 GB. Server health stayed `ok`, and no store jobs were created in the window. |
| 21:05:06 | restart | health `ok`, `0.2.0+e3eec95`, schema `010_sources`. Log: `Decomposer: anthropic claude-haiku-5-5`. Stuck jobs: none (727 `complete`, 0 pending or decomposing). |
| ~21:07 | smoke test | Scratch operator and agent `ebf6b766-…`. `remember` → 201 `complete`, store `b774b7e9-…`, 1 atom with a `source_quote`, no flags, 2.30 s wall (first call after start). `recall` → 200, 0.18 s, stored atom found, `source_id` set, `origin_class` `unspecified`. `GET /stores/{id}/status` → `complete`, 1 atom. `GET /agents/me` → the scratch agent. Purge → 1 atom deleted. The departed agent row, its store job, address and operator (with API key) were deleted in a guarded transaction. Back to 14 agents and 13,061 atoms. |
| ~21:08 | per-agent recall | `scripts/phase0/5e_latency.sql`: all 13 active agents with atoms return their candidates (2–20 rows), 0.0–199 ms execution, cold cache |

**First real store on the new server (0.5.0 client), 2026-11-23 21:15:18 UTC:** agent clio (`ef56be05-…`), store `7a59e70f-c78b-4f1b-bf7a-bfa01a5ce017`, source `ba462929-…` (133 chars, `origin_class` `unspecified`, speaker = agent name). The old client sends no `wait`, so it got the default synchronous path: `POST /remember` → **201**, status `complete`, 1 part. Server time 1.96 s (created → completed); `timings_ms`: decompose 1,444, embed 509, persist 100. **2 atoms** (`b0ec9230-…`, `84542233-…`), both `episodic`, both with a `source_quote`, **0 flags**, `decomposer_version` `anthropic:claude-haiku-5-5:prompt_v2`; 0 duplicates merged; 0 `store_failures`.
- Background lifecycle: 8 checks (about 1.0–1.8 s each). Edges created: 2 `supersedes`, 3 `narrows`, plus `related` similarity edges. No atom was deactivated (the 3 targets are still active). One `narrows` edge links the store's two atoms to each other: the candidate query excludes only the atom itself, unchanged from Phase 0. Noted for Phase 2.

**Still to do after G2:**
- 24 hours after the 21:05 restart: stores attempted, complete and failed; `remember` latency p50 and p95; flag counts on new atoms by flag; decomposer and lifecycle errors in the logs.
- Disk: at Tom's request the unused April image `mnemo-mnemo-server:latest` (`8f0e5b51ce2b`, 23.9 GB, no container) was removed at ~21:09. Free space is now 49 GB. The previous image `63af8ab7674b` (`7a0c676`) was untagged by the rebuild and is **gone** (removed outside this work within minutes). Rolling back now means rebuilding from `7a0c676`. 12.4 GB of build cache is still on disk.

## Open items

- **G3:** client publish questions (below).
- Done: Tom hand-labelled the 47-row relation check (`ccbd8ef`), and the bake-off is rescored against those labels.
- PRs (open, not merged): `mnemo-server` #24 `v2/phase1` → `main` (retargeted after #23 merged); `mnemo-client` #2 `v2/phase1` → `main`; `inforge-ai/mnemo-mcp` #1, the "superseded" README notice.

### G3 questions (client publish)

What I can see locally: this machine's Claude Code runs `uvx --from mnemo-ai[mcp] mnemo-mcp` with `MNEMO_DEFAULT_AGENT_ID` set, and several Hermes-managed `uvx --from mnemo-ai[mcp]` processes are running.

1. How does each agent install the client: the uvx configs here and in Hermes, any pinned versions, and the remote agents (nels-*, eaara)?
2. Which agent upgrades first?
3. 0.6.0 needs the Phase 1 server (`/agents/me`). Publish only after G2? (Proposed: yes.)
