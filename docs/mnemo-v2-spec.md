# Mnemo v2 — Phase 0 spec: stop the bleeding

**Date:** 2026-11-21
**For:** a Claude Code session on the server that hosts mnemo-server, with the production `.env` and Postgres available.
**Owner:** Tom Davis. Where this spec says **STOP**, report and wait for Tom's reply before continuing.
**Context docs:** `mnemo-v2-plan.md`, `mnemo-server-code-review-2026-11-21.md` (both in the Mnemo v2 project); `docs/rewrite-notes-2026-11-21.md` in the repo.

## Goal

Stop mnemo from losing or silently altering memories, clean the production data, and leave the repo in a state where Phase 1 can be built and tested. No new features.

## Background you need

- Atoms are soft-deleted (`atoms.is_active = false`), never hard-deleted except when a departed agent is purged. Deactivated rows are still in the table.
- Three background steps in `mnemo/server/services/consolidation.py` destroy or distort data every hour:
  - `_deactivate_faded_atoms` deactivates atoms whose decayed confidence is below 0.05 (unaccessed episodic atoms go at about 8 weeks).
  - `_merge_duplicates` deactivates the *newer* of any same-type pair with cosine > 0.90. Each merge is logged in `access_log` with `action = 'merge'` and `metadata->>'absorbed_atom_id'`.
  - `_cluster_and_generalise` creates immortal "Generalised from N observations" atoms that are a copy of one member's text.
- `_prune_dead_edges` then deletes edges touching inactive atoms. Those edges are gone for good.
- Production also holds about 28k atoms under `locomo-conv-*` agents from March benchmark runs.
- The agents' memories contain private material. **Do not copy atom text or raw feedback into the repo, commit messages, logs or reports.** Counts and ids only.

## Rules

1. Work on a branch `v2/phase0` off `main`. Small commits. Open a PR; do not merge to `main` or deploy without Tom's go-ahead.
2. Take and verify a backup before the first write to production (work item 2).
3. Every production data change runs as: dry-run count → **STOP** → execute inside a transaction → verify counts → record in the report.
4. If something in this spec does not match what you find, say so in the report and ask. Do not improvise on production.
5. Out of scope: decomposer changes, ranking changes, new endpoints, sharing, the MCP client. Those are later phases.

## Work items

### 1. Baseline report (read-only)

Write `docs/phase0-report-2026-11.md` and keep adding to it through the phase. First section, before any change:

- How the server is deployed and restarted on this host; current commit; `pgvector` and Postgres versions; the definition of the `atoms` embedding index.
- Per agent: total atoms, active atoms, inactive atoms; agent status (active/departed).
- Inactive atoms split by cause: merged (id appears as `absorbed_atom_id` in `access_log`), other.
- Consolidation history from `access_log` (`action = 'consolidation'`): runs per day over the last 60 days and the counts in each. Gaps mean runs failed or timed out; check server logs for "Consolidation run failed".
- `store_jobs` by status, with the age of the oldest `pending`/`decomposing` row; `store_failures` count.
- `atoms` by `decomposer_version`; count of `source_type = 'consolidation'` atoms; max and p99 of `confidence_alpha`.
- Edge count by type; count of self-loops (`source_id = target_id`).
- `decomposer_usage`: total rows, and sum of `cache_read_input_tokens` (tells us whether prompt caching ever worked).
- Which `MNEMO_*` settings are overridden in the environment (names only, no secrets), in particular `MNEMO_LIFECYCLE_DETECTION_ENABLED`.
- Which migrations are recorded in `schema_migrations`, and whether `edges.metadata` and `lifecycle_dlq` exist.

**STOP — gate G1:** send Tom the baseline section.

### 2. Backup

- `pg_dump` the production database in custom format to a dated file outside the repo. Verify with `pg_restore --list` and by restoring it into a new database `mnemo_eval` (this restored copy is reused in item 5).
- Record file path, size and row counts of `atoms`, `edges`, `agents` in both databases.

### 3. Code: stop destructive background work

In `config.py` add four settings, all default **False**:

- `consolidation_decay_enabled`
- `consolidation_generalise_enabled`
- `consolidation_merge_enabled`
- `consolidation_prune_edges_enabled`

`run_consolidation` skips each disabled step and reports 0 for it. Leave departed-agent purge and the audit log entry as they are. Update `tests/test_consolidation.py` so existing tests enable the flags they exercise, and add a test that with defaults a faded atom, a duplicate pair and an edge to an inactive atom all survive a run.

Also add `recall_confidence_floor_enabled` (default **False**). When false, `atom_service.retrieve` and both shared-recall functions in `view_service.py` skip the `confidence_effective >= min_confidence` filter. Reason: clients send `min_confidence = 0.1` explicitly, which hides restored and older atoms; confidence still affects ranking through `composite_score`. Add a test.

Do not change store-time dedup code. Instead set `MNEMO_DUPLICATE_SIMILARITY_THRESHOLD=0.97` in the production environment, so only near-verbatim repeats merge until Phase 2 replaces this logic. Recall already collapses near-duplicates at 0.95 on read.

### 4. Migrations that actually run

- Add a `mnemo migrate` CLI command that applies pending `migrations/*.sql` in filename order, each in its own transaction, and records the version. It connects with `MNEMO_MIGRATION_DATABASE_URL` (a role allowed to run DDL), falling back to `MNEMO_DATABASE_URL`.
- Replace the heuristic detection in `migration_service.run_migrations` with: list pending migrations; if any, log an error naming them. Add setting `require_migrations` (default True) that makes startup fail when migrations are pending.
- Make `schema.sql` plus `mnemo migrate` produce the production schema on an empty database: `schema.sql` records versions 001–005 in `schema_migrations` (it already contains their effects), and 006 onwards are applied by the command. Confirm migration 006 is idempotent against production.
- New migration `007_deactivation_tracking.sql`:
  - `ALTER TABLE atoms ADD COLUMN deactivation_reason TEXT, ADD COLUMN restored_at TIMESTAMPTZ;`
  - `ALTER TABLE edges ADD CONSTRAINT edges_no_self_loop CHECK (source_id <> target_id);` preceded by deleting existing self-loops.
- Check: build a scratch database from `schema.sql` + `mnemo migrate`, and diff `pg_dump --schema-only` against production after migration. Report any difference.

### 5. Production data changes

Stop the server for this window. Order matters.

**5a. Apply migrations** 006 (if missing) and 007.

**5b. Classify inactive atoms.**
```sql
UPDATE atoms a SET deactivation_reason = 'merged'
FROM access_log l
WHERE l.action = 'merge'
  AND (l.metadata->>'absorbed_atom_id')::uuid = a.id
  AND a.is_active = false;

UPDATE atoms SET deactivation_reason = 'decay_or_deleted'
WHERE is_active = false AND deactivation_reason IS NULL;
```

**5c. Restore.** Candidates: `is_active = false`, `deactivation_reason = 'decay_or_deleted'`, owning agent `status = 'active'`, agent not a `locomo-conv-*` agent. Merged atoms stay inactive (restoring them would reintroduce duplicates); atoms of departed agents stay inactive.

Dry run: counts per agent.

**STOP — gate G2.** Tell Tom the per-agent counts and these two caveats:
- The DELETE atom endpoint leaves no audit trail, so an atom an agent deliberately deleted cannot be told apart from one that decayed. Ask whether any agent or script has used delete (the MCP server exposes no delete tool; the Python client and REST API do).
- Edges of these atoms were pruned, including any `supersedes` edges, so some restored atoms may be stale facts. They will rank low but will be visible.

On approval: `UPDATE atoms SET is_active = true, restored_at = now() WHERE <candidates>`. Do not touch `last_accessed`, `access_count` or confidence.

**5d. Remove the eval corpus from production.**
- List agents matching the eval pattern (`name LIKE 'locomo-conv-%'`, plus anything else that is plainly benchmark data) with atom counts.

**STOP — gate G3:** send the list; Tom confirms which agents go.

- In `mnemo_eval` (the restored copy from item 2): delete every agent that is *not* on the confirmed list, so no private memory remains in the eval database. Verify only eval agents and their atoms remain.
- In production: delete capabilities where grantor or grantee is on the list, then delete those agents (atoms, edges, views cascade). Follow the order used in `consolidation._purge_departed_agents`.
- Verify: production atom count dropped by exactly the eval atom count; no non-eval agent lost a row.

**5e. Rebuild the vector index.**
- Drop `idx_atoms_embedding` (ivfflat) and create an HNSW index with `vector_cosine_ops`. Requires pgvector ≥ 0.5; check the installed version and its documentation for parameters.
- In `database.py` replace `SET ivfflat.probes = 10` with the HNSW equivalent (`hnsw.ef_search`). If the installed pgvector supports iterative index scans, enable them so per-agent filters do not starve results; confirm the setting name against the docs for that version.
- Put the index change in migration `008_hnsw_index.sql` and update `schema.sql`.
- `VACUUM ANALYZE atoms; VACUUM ANALYZE edges;`

**5f. Restart** on the `v2/phase0` build with the new environment setting. Check `/v1/health`, then run one recall per active agent through the API using that agent's own key or the admin key, and record only the number of results and latency.

### 6. Regression fixtures

Create `tests/fixtures/v2/` with one JSON file per case and a test module `tests/eval/test_v2_fixtures.py` under the existing `eval` marker. Each case: input text(s), optional `remembered_on`, and assertions on the resulting atoms or recall. Mark cases the current system fails as `xfail(strict=True)` with the phase expected to fix them.

All fixture text must be **synthetic**, written to reproduce the failure pattern. Do not copy from `mnemo-feedback-raw.json` or from stored atoms.

Minimum cases:
- Attribution: a remark addressed to agent A about A is stored as a property of A, not of the system being discussed.
- Attribution: an agent's own state ("I am not connected to X") is not stored as the user's state.
- Abbreviation: a domain abbreviation in the input appears verbatim in the atom and is not expanded.
- Negation pair: "X uses A" then "X no longer uses A"; recall for X's tool returns the second and not the first.
- Updated number: "score is 76.1%" then "score is 82.1%, the earlier figure was wrong"; recall returns 82.1%.
- Planned then done, stored a month apart.
- Validity window: "this week only" is not returned as a current fact when recalled two months later.
- Read-your-writes: a recall immediately after `remember` finds the memory.

### 7. Small safe fixes (only these)

- `atom_service.store_explicit`: pass `atom_type` to `_check_duplicate`.
- `atom_service.store_from_text`: record `decomposer_version` from what actually ran (the LLM path returns usage; the regex fallback does not), not from the presence of the API key.
- `models.RetrieveRequest`: bound `max_results` (1–50), `expansion_depth` (0–2) and `min_confidence` (0–1).
- `_create_similarity_edges`: skip pairs with the same id.

Each with a test.

## Exit criteria

- With default settings, a consolidation run deactivates nothing, merges nothing, creates no atoms and deletes no edges. Covered by a test and confirmed on production after 24 hours (`access_log` consolidation rows show zeros).
- Restored atom counts match the G2 dry run.
- No eval agents in production; `mnemo_eval` contains only eval agents.
- Fresh database from `schema.sql` + `mnemo migrate` matches the production schema.
- Vector index is HNSW; recall works for every active agent.
- Full test suite passes; fixture cases exist and are xfail where expected.
- `docs/phase0-report-2026-11.md` is complete: before/after counts, every gate decision, anything that differed from this spec, and the open questions below answered where the data allows.

## Questions the report should answer for Phase 1

1. Has hourly consolidation been completing, or timing out? Since when?
2. Did prompt caching ever register a cache read?
3. How many atoms carry `haiku_v1` but look like regex output (for example arc atoms or very long multi-sentence atoms)? Count only.
4. How many `store_jobs` were left stuck, and over what period?
5. Is lifecycle detection enabled in production, and how many `supersedes` / `tension_with` / `narrows` edges exist?
