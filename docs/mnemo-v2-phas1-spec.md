# Mnemo v2 — Phase 1 spec: trustworthy writes

**Date:** 2026-11-22
**For:** the Claude Code session on the mnemo server.
**Owner:** Tom Davis. **STOP** means report and wait for Tom.
**Prerequisite:** Phase 0 merged (`v2/phase0`), 24-hour consolidation check passed.
**Context:** `mnemo-v2-plan.md`, `mnemo-v2-phase0-spec.md`, `docs/phase0-report-2026-11.md`, `docs/rewrite-notes-2026-11-21.md`.

## Goal

An agent that calls `remember` can see exactly what was stored, trust that it says what the source said, and find it again straight away. Nothing is stored silently wrong, and nothing is silently lost.

## What Phase 0 changed in the plan

- **No job worker.** Production sees about four stores a day and has never lost one. `remember` becomes synchronous by default instead.
- **No vector index.** Exact search is fast at this size.
- **Decision models stay out of the production path in this phase.** Phase 1 builds the bake-off and picks one; Phase 2 puts it to work. Grounding in Phase 1 is done with deterministic checks.

## Rules

1. Branch `v2/phase1` off `main` in `mnemo-server`; a matching branch in `mnemo-client`. PRs, no direct merges, no deploy without Tom.
2. Production data changes follow the Phase 0 pattern: dry run, **STOP**, guarded execute, verify.
3. No atom text, source text or raw feedback in the repo, logs, commits or reports. Fixtures are synthetic.
4. **No production memory text goes to any provider other than Anthropic.** The bake-off uses synthetic data and the LoCoMo corpus in `mnemo_eval` only.
5. Do not re-enable any consolidation flag.
6. Out of scope: dedup and supersession logic, ranking, sharing, confidence recalibration, decision models in the write or read path.

## Work items

### 1. Test hygiene (do first)

- Tests must not read the production `.env`. Use a separate `.env.test`; fail fast if `MNEMO_DATABASE_URL` does not contain `test`.
- LLM calls are mocked by default. Anything that needs a live model carries the `eval` marker.
- Add a deterministic fake embedder (setting `embedding_backend=fake`) for unit tests, so the suite runs without the gated model. The real model is used under the `eval` marker.
- Fix or remove `tests/test_client.py` (imports a module that left the repo) and the flaky `test_stats_includes_date_range`.
- Add a CI workflow: Postgres with pgvector, `schema.sql` + `mnemo migrate`, full non-eval suite.
- Rebuild the local test database (it went with the Phase 0 container).

### 2. Design note — **STOP, gate G1**

Before writing feature code, write `docs/phase1-design.md` covering: migration 010, request and response shapes, the full text of decomposer prompt v2, the deterministic checks, and the MCP tool signatures. Tom will have it reviewed. Items 3–7 below are the brief for it.

### 3. Sources and provenance

Migration `010_sources.sql`:

- `sources`: `id`, `agent_id` (FK, cascade), `store_id`, `text`, `speaker`, `addressee`, `channel`, `origin_class`, `remembered_on`, `created_at`.
- `origin_class` is one of: `user_stated`, `professional`, `measurement`, `document`, `agent_inference`, `unspecified` (default).
- `atoms` gains `source_id` (FK, `ON DELETE SET NULL`), `source_quote TEXT`, `verification JSONB`.
- Existing atoms keep `source_id` NULL. No backfill.

`RememberRequest` gains optional `speaker`, `addressee`, `channel`, `origin_class`. When `speaker` is absent it defaults to the calling agent's name: in practice the text is a note written by the agent.

The source row is written first, in its own transaction, before decomposition. If decomposition then fails, the text is not lost.

### 4. Synchronous, inspectable remember

- `POST /agents/{id}/remember` gains `wait` (default **true**). It returns when storage is complete:
  ```json
  {"status": "complete", "store_id": "...", "source_id": "...",
   "atoms_created": 3, "duplicates_merged": 1,
   "atoms": [{"id": "...", "atom_type": "episodic", "text_content": "...",
              "remembered_on": "...", "merged_into_existing": false, "flags": []}],
   "warnings": []}
  ```
- `wait=false` keeps today's queued behaviour.
- On decomposer failure after retries: HTTP 502, `status: "failed"`, the `store_id`, and a plain error. The source row stays. Add `POST /stores/{store_id}/retry`.
- `GET /stores/{store_id}/status` also returns the atom list.
- Restructure `store_background` / `store_from_text`: decompose and embed with no database connection held; then one transaction for all inserts. Embed atoms in one batch call.
- Lifecycle detection stays in the background after the response. Keep a reference to each background task. On startup, mark `store_jobs` rows stuck in `pending`/`decomposing` for over 10 minutes as `failed`.

### 5. Decomposer v2

**Interface.** A `Decomposer` protocol with implementations selected by `MNEMO_DECOMPOSER_PROVIDER` (`anthropic`, `openrouter`, `regex`) and `MNEMO_DECOMPOSER_MODEL`. `decomposer_version` records `provider:model:prompt_version`.

**Failure behaviour.**
- Retry twice with backoff on timeouts, 429 and 5xx.
- Check the stop reason; a truncated response is a failure, not something to parse.
- Split inputs over about 8,000 characters on paragraph boundaries and decompose each part.
- **No regex fallback** unless the provider is explicitly `regex`.
- Use the provider's structured-output or tool mechanism so the reply is schema-valid; skip a malformed item with a warning, do not discard the batch.

**Prompt v2 requirements.**
- A fixed preamble built from the request: who wrote the note, who "I" and "you" refer to, and the date. Example: "This note was written by the agent `clio`. 'I' means `clio`. 'You' means `<addressee>`."
- Every atom states its subject explicitly. A remark made *to* someone about themselves is a fact about that person, not about the topic under discussion.
- An agent's own state is never recorded as the user's state.
- Abbreviations, tickers, codes and proper nouns are copied verbatim. Never expand an abbreviation.
- Talk *about* a system (meta-conversation) yields fewer atoms: only decisions, facts and plans, not the back-and-forth.
- Each atom carries `source_quote`: the shortest verbatim passage of the source that supports it.
- Drop the benchmark-specific examples in the current prompt. Keep the date-resolution and `remembered_on` rules.
- Types stay `episodic | semantic | procedural`.
- Remove `cache_control`; the prompt is below the cacheable size and Phase 0 showed zero cache reads.

**Deterministic checks after decomposition.** Results go in `atoms.verification` and in the response `flags`.
- `ungrounded_quote`: `source_quote` is not a substring of the source (compare after normalising whitespace and case).
- `abbreviation_altered`: an all-capitals token of 2–6 characters in the quote does not appear in the atom text.
- `no_subject`: the atom starts with a pronoun or has no named subject (simple heuristic; record, do not block).

Flagged atoms are stored one confidence band lower and returned with their flags. They are not dropped.

### 6. Read-side quick wins

- `RetrieveRequest` gains `since`, `until` (applied to `COALESCE(remembered_on, created_at)`) and `atom_types`.
- New `POST /agents/{id}/recent`: newest atoms first by the same date, with `limit`, `since`, `until`, `atom_types`, `domain_tags`. No embedding, no access-count update.
- New `GET /agents/me`: the agent identified by the key (id, name, address).
- Recall and recent responses include `source_id` and `origin_class`.

### 7. MCP server (`mnemo-client`)

- `agent_id` becomes optional everywhere and defaults to the key's own agent via `/agents/me`. When given, accept UUID, name or address. Errors state the accepted forms.
- `mnemo_remember`: add `speaker`, `addressee`, `channel`, `origin_class`, `remembered_on`. Return the stored atoms and any flags as short lines.
- `mnemo_recall`: add `since`, `until`, `atom_types`. Default `verbosity` becomes `full` with a per-atom character cap; the current default returns only each atom's first sentence.
- New tools: `mnemo_recent`, `mnemo_forget` (delete by atom id; now audited as `deleted_by_agent`), `mnemo_store_status`.
- Update the server instructions string and README. Mark the separate `mnemo-mcp` repo as superseded.
- **STOP — gate G3** before publishing: ask Tom how each agent installs the MCP server and which to upgrade first.

### 8. Harder fixtures

Add to `tests/fixtures/v2/`, all synthetic. Each live case runs 5 times and must pass 5 of 5.

- Attribution with no speaker label in the text, relying on the request's `speaker` / `addressee`.
- Attribution inside a note about a third system, where the system name is the grammatical topic.
- An abbreviation next to a real listed ticker in a finance sentence, where a common expansion exists and is wrong.
- A long note (over 8,000 characters) with a fact in the last paragraph.
- A simulated provider failure: the response is a 502, the source row exists, and no atoms are stored.
- `read_your_writes` flips from xfail to pass.

### 9. Decision-model bake-off (no production integration)

Deliverable: `scripts/eval/decision_bakeoff.py` and `docs/phase1-bakeoff.md`.

- **API:** OpenRouter Decisions, `POST https://openrouter.ai/api/alpha/decisions` with `model`, `state` and typed `questions`. List candidates with `GET /api/v1/models?output_modalities=decisions`. It is an alpha API: read the current docs before coding and record the date.
- **Candidates:** TypeSafe Jev plus three or four others from that list. Also one generative baseline (the decomposer model) answering the same questions as JSON.
- **Task A, grounding (yes/no):** "Is this atom fully supported by this quote?" Positives are decomposer outputs on synthetic and LoCoMo text. Negatives are made by perturbation: change a number, expand an abbreviation, swap the subject, negate.
- **Task B, relation (choice):** `same | update | contradicts | narrows | unrelated` for a pair of atoms. About 200 synthetic pairs written to a spec, balanced across classes. Tom hand-labels a random 40 as a check on the labels.
- **Metrics per model and task:** accuracy and per-class recall; calibration (Brier score, expected calibration error, reliability table); sensitivity to option order and to label wording (same items, permuted); latency p50/p95; cost per 1,000 decisions.
- **Also:** run the Phase 1 fixtures against two generative decomposers (current Haiku 4.5 and the current Haiku release) and report pass rates.
- Needs `OPENROUTER_API_KEY`. Ask Tom; keep it out of the production container's environment for now.

### 10. Deploy — **STOP, gate G2**

Show Tom: migration 010 dry run, the full suite result, fixture pass rates, and a before/after of `remember` latency on synthetic input. On approval: backup, migrate, rebuild, restart, health check, one synthetic remember-then-recall on a scratch agent, then delete the scratch agent.

## Exit criteria

- `remember` returns the atoms it stored; an immediate recall finds them.
- A decomposer failure is visible to the caller and loses nothing.
- Every new atom has a source row and a quote; ungrounded or altered atoms are flagged.
- Attribution and abbreviation fixtures, including the harder ones, pass 5 of 5.
- No database connection is held during a model call.
- The suite runs with no network and no production credentials; CI is green.
- Agents can call the MCP tools without knowing a UUID.
- `docs/phase1-bakeoff.md` recommends a decision model with numbers behind it.
- `docs/phase1-report.md` records gates, deviations and open questions.

## Questions the report should answer for Phase 2

1. What share of new atoms are flagged, by flag?
2. How does `remember` latency split between the model call, embedding and the database?
3. Which decision model wins each task, and is one model good enough for both?
4. How sensitive are the candidates to option order and label wording?
5. How many of the 430 `merge_pairs` have a kept atom that is itself inactive (merge chains)?
