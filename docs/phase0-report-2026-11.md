# Mnemo v2 Phase 0 — Report (2026-11)

Spec: `docs/mnemo-v2-spec.md`. Plan: `docs/superpowers/plans/2026-11-21-v2-phase0.md`.
This report holds counts and ids only. It contains no atom text and no feedback text.

---

## 1. Baseline (before any change), taken 2026-11-21

All queries ran against production in a read-only transaction (`default_transaction_read_only=on`).

### Deployment
- **Process:** docker compose service `mnemo-server`, container `mnemo-server-mnemo-server-1`.
  - Image built 2026-04-27 with `BUILD_COMMIT=81a492c`, the same commit as current `main` HEAD.
  - Up 3 days at the time of the check.
  - `/v1/health` returns `ok`, version `0.2.0+81a492c`, `schema_version` `006_lifecycle_edges`.
- **Restart:** `docker compose up -d --build` with `BUILD_COMMIT` set. The container reads `.env`.
  - `MNEMO_DATABASE_URL` is overridden by `MNEMO_DOCKER_DATABASE_URL`, which points at Postgres on the host through `host.docker.internal`.
- **Port binding:** the uncommitted local `docker-compose.yml` change binds the port to `0.0.0.0:8000` (committed version: `127.0.0.1`) and adds public DNS servers. The running container is on `0.0.0.0:8000`.
- **Postgres:** 16.15 (Ubuntu pgdg24.04). **pgvector:** 0.8.2. That is ≥0.5, so HNSW works, and ≥0.8, so iterative index scans work.
- **Embedding index:** `idx_atoms_embedding ON atoms USING ivfflat (embedding vector_cosine_ops) WITH (lists='100')`.

### Overridden `MNEMO_*` settings (names only)
- `.env` and container: `MNEMO_DATABASE_URL`, `MNEMO_DOCKER_DATABASE_URL`, `MNEMO_ADMIN_KEY`, `MNEMO_LIFECYCLE_DETECTION_ENABLED`.
- **`MNEMO_LIFECYCLE_DETECTION_ENABLED=true`**, so lifecycle detection is on in production.
- Every other setting is at its code default, including `duplicate_similarity_threshold=0.90` and `min_effective_confidence=0.05`.

### Migrations
- `schema_migrations` rows:
  - `001_admin_schema`
  - `002`
  - `002_rbac_lite`
  - `003`
  - `003_sharing_scope`
  - `004_fix_effective_confidence_volatility`
  - `005_remembered_on`
  - `006_lifecycle_edges` (applied 2026-04-27)
- **Differs from spec:** there are stray rows `002` and `003`, apparently from an older naming scheme. They are harmless, but `mnemo migrate` must ignore versions that don't match a file.
- `edges.metadata` exists, and so does `lifecycle_dlq`. Migration 006 is fully applied.

### Totals
| atoms | edges | agents | access_log |
|---:|---:|---:|---:|
| 39,672 | 44,453 | 25 | 29,638 |

### Atoms per agent (non-eval)
All 14 agents are `active`. None are departed.

| agent | total | active | inactive |
|---|---:|---:|---:|
| toms-claude-desktop | 3,731 | 1,438 | 2,293 |
| eaara | 2,357 | 1,848 | 509 |
| nels-claude-desktop | 2,281 | 600 | 1,681 |
| clio | 2,005 | 832 | 1,173 |
| tapio | 1,459 | 771 | 688 |
| illmarinen | 624 | 164 | 460 |
| amphion | 163 | 112 | 51 |
| inforge-researcher | 107 | 88 | 19 |
| lloyd | 69 | 54 | 15 |
| toms-claude-code | 43 | 39 | 4 |
| nels-hermes-agent | 26 | 9 | 17 |
| agent1 | 3 | 0 | 3 |
| zethus | 2 | 2 | 0 |
| mimir | 0 | 0 | 0 |
| **sum** | **12,870** | **5,957** | **6,913** |

### Eval corpus (`locomo-conv-%`)
11 agents, all with status `active`. They hold 26,802 atoms: 3,064 active and 23,738 inactive. The spec estimated about 28k. 12,870 + 26,802 = 39,672, which matches the total.

### Inactive atoms by cause
"Merged" means the atom id appears as `absorbed_atom_id` in an `access_log` merge row.

| set | source_type | merged | other |
|---|---|---:|---:|
| non-eval | consolidation | 3,458 | 0 |
| non-eval | direct_experience | 430 | 2,956 |
| non-eval | arc | 21 | 48 |
| **non-eval total** | | **3,909** | **3,004** |
| eval | consolidation | 19,972 | 0 |
| eval | direct_experience | 45 | 3,721 |

**Finding:** 89% of merged non-eval atoms (3,458 of 3,909) are `consolidation`-generated "Generalised" copies, not real memories. `_cluster_and_generalise` created a copy and `_merge_duplicates` then absorbed it, over and over. Only 430 real non-eval memories were lost to merging. The likely restore pool (non-eval, not merged) is 3,004: 2,956 `direct_experience` and 48 `arc`. **Arc atoms are synthetic summaries. Whether they should be restored is a question for G2.**

### Consolidation history (`access_log`, `action='consolidation'`)
- 5,658 runs, from 2026-03-30 to 2026-11-21 21:48, the last hour.
- About 24 runs per day every month since April:
  - Apr: 716
  - May: 739
  - Jun: 717
  - Jul: 740
  - Aug: 741
  - Sep: 717
  - Oct: 741
  - Nov so far: 500
- **Last 60 days:** runs on all 61 days. Some days show 22–23 runs, which fits the loop drifting (sleep plus run time), not failures.
- Totals for the last 60 days:
  - decayed 161
  - clustered 6
  - merged 1
  - pruned 1,413
- All-time totals:
  - decayed 6,725
  - clustered 20,083
  - merged 20,060
  - pruned 71,539
  - purged 0
- The merge log has 23,926 rows: Mar 4,168, Apr 7,177, May 7,410, Jun 5,169, then one each in Sep and Oct. Merge churn stopped after June.
- **Discrepancies:**
  - Merge rows in `access_log` (23,926) exceed the `merged` sum in the consolidation rows (20,060).
  - `consolidation`-source atoms (23,540) exceed the `clustered` sum (20,083).
  - Both gaps are probably from runs in March before audit rows were written, or from manual runs. Not investigated further.
- Server logs cover 2026-04-27 to now and contain **0** "Consolidation run failed" lines. The 11 tracebacks in the logs come from `llm_decomposer` warnings, and 20 timeout lines come from `lifecycle_service`. None are from consolidation.
- Edges still touching inactive atoms: 0, so pruning keeps up.

### Store pipeline
- `store_jobs`: 1,008 rows, all `complete`, from 2026-03-29 to 2026-11-21. None are `pending` or `decomposing`.
- `store_failures`: 0.

### Decomposer and confidence
| decomposer_version | atoms | active |
|---|---:|---:|
| regex_v1 | 23,680 | 177 |
| haiku_v1 | 15,992 | 8,844 |

| source_type | atoms | active |
|---|---:|---:|
| consolidation | 23,540 | 110 |
| direct_experience | 16,063 | 8,911 |
| arc | 69 | 0 |

`regex_v1` is nearly all consolidation atoms, which take the column default. Split by set:
- Non-eval: 9,216 `haiku_v1` and 3,654 `regex_v1`.
- Eval: 6,776 `haiku_v1` and 20,026 `regex_v1`.

`confidence_alpha`:
- max **198,429**
- p99 269
- p50 31

The alpha values have blown far past any sensible range. Alpha growth is unbounded.

### Edges
| edge_type | count |
|---|---:|
| related | 39,057 |
| narrows | 3,193 |
| tension_with | 2,031 |
| supersedes | 137 |
| generalises | 23 |
| motivated_by | 12 |

**Self-loops: 318.**

### `decomposer_usage`
- 996 rows, from 2026-03-29 to 2026-11-21.
- Model: `claude-haiku-4-5-20251001` only.
- `sum(cache_read_input_tokens)` = **0** and `sum(cache_creation_input_tokens)` = 0. No row has ever recorded a cache read.
- 1,008 complete store jobs against 996 usage rows: 12 stores ran without logging usage, presumably on the regex fallback.

---

## Answers to Phase 1 questions (from the baseline)

1. **Is consolidation completing?** Yes. It has run about hourly without failure since at least 2026-04-27, the start of the server logs. `access_log` shows about 24 runs per day back to April. The damage came from what the runs did, not from timeouts.
2. **Did prompt caching ever register a cache read?** No. Across all 996 usage rows, cache read and cache creation tokens are both 0.
3. **How many `haiku_v1` atoms look like regex output?**
   - 11 `arc` atoms.
   - 47 atoms over 500 characters.
   - 53 atoms with 4 or more sentences.
   - These sets overlap. Also, 12 stores have no usage row.
4. **How many `store_jobs` are stuck?** None. All 1,008 jobs are `complete`.
5. **Is lifecycle detection enabled?** Yes. There are 137 `supersedes`, 2,031 `tension_with` and 3,193 `narrows` edges.

## Differences from the spec and the rewrite notes
- The eval corpus is 26,802 atoms across 11 agents, not about 28k. All 11 have status `active`.
- Per-agent counts differ slightly from the rewrite notes: clio has 2,005, not 1,959. Agents the notes don't mention: `eaara` (2,357), `nels-claude-desktop` (2,281) and four small ones.
- `schema_migrations` contains stray `002` and `003` rows.
- Most merged atoms are consolidation copies, so the merged/other split matters less than the spec assumed.
- `confidence_alpha` reaches 198k. That is out of scope for Phase 0, but it bears on the confidence-floor change and on recalibration.
- The production port is bound to `0.0.0.0` through an uncommitted compose change.

**Gate G1:** approved by Tom on 2026-11-21, with amendments (recorded under G1 decisions below).

---

## G1 decisions (Tom, 2026-11-21)
- **5c:** restore only `source_type='direct_experience'`. Arc atoms stay inactive with `deactivation_reason='arc'`.
- **Deletion caveat:** resolved. The non-merged inactive atoms are exactly the all-time decayed count, 6,725, so none were deliberately deleted. At G2, give per-agent counts split by merged / decayed.
- **New 5c-bis:** count active `consolidation` atoms per non-eval agent. At G2, propose deactivating them with `deactivation_reason='consolidation_copy'`.
- **5e:** drop `idx_atoms_embedding` and do **not** create an HNSW index. Remove `ivfflat.probes`. Confirm per-agent latency with EXPLAIN ANALYZE.
- `mnemo migrate` ignores `schema_migrations` rows that match no file.
- The docker-compose port binding is unchanged; Tom will decide.
- Agents owned by others (`nels-*`, `eaara`): *"include from the restore"*. This is ambiguous (include *in* or exclude *from*), so G2 shows both totals.

---

## 2. Backup

| item | value |
|---|---|
| file | `~/backups/mnemo-prod-2026-11-21.dump` (custom format, mode 600) |
| size | 139,474,330 bytes (133 MB); database size 394 MB |
| sha256 | `46a93f4433d4ccd873db0c491a21add89c78408dd4b0a059bf696ef7531ae7ac` |
| `pg_restore --list` | 18 TABLE DATA entries, one for every table in `public` |
| dumped as | role `mnemo`, which can read every table; took 18 s |

| rows | production at dump time | restored copy |
|---|---:|---:|
| atoms | 39,672 | 39,672 |
| edges | 44,455 | 44,455 |
| agents | 25 | 25 |

- **Differs from spec:** the restore into `mnemo_eval` on the host Postgres is **not done**.
  - Role `mnemo` has neither CREATEDB nor superuser, every table is owned by `postgres`, and sudo needs a password.
  - Instead, the dump was restored into a local container (`pgvector/pgvector:0.8.2-pg16`, bound to 127.0.0.1:55432) as `mnemo_prodcopy`. Its counts are in the table above.
  - That copy is used only for rehearsals. It holds private data, so it gets dropped after Phase 0.
  - **Needed from Tom:** a superuser step to create `mnemo_eval` on the host (commands under "Needed from Tom" below).
- Edges went 44,453 → 44,455 between the baseline and the dump (live writes).

---

## 3, 4, 6, 7. Code (branch `v2/phase0`)

| commit | item |
|---|---|
| `9f1cd58` | 3: consolidation step flags and recall confidence floor flag, all default off |
| `35e5cd1` | 4 and 5e (code): `mnemo migrate`, startup check, migrations 007 and 008 (008 later renumbered to 009), `ivfflat.probes` removed |
| `07f0f09` | 7: four small fixes |
| `00f8676` | 6: v2 regression fixtures |

Notes:
- **Item 3:** `recall_all_shared` had its own hardcoded `>= 0.05` floor. It is now behind the same flag.
- **Item 3, production config:** `MNEMO_DUPLICATE_SIMILARITY_THRESHOLD=0.97` is not set yet. It goes into `.env` at 5f.
- **Item 4:**
  - `mnemo migrate [--dry-run] [--to VERSION] [--database-url URL]`. `--to` lets 5a apply only 007, leaving 008 for 5e.
  - Startup fails while migrations are pending (`MNEMO_REQUIRE_MIGRATIONS`, default true).
  - The legacy `002` and `003` rows came from those migrations recording themselves under short names.
- **Item 5e (code):** `009_drop_ivfflat_index.sql` (first committed as 008). `schema.sql` keeps the index as part of the 001–005 baseline, and 009 drops it.

### Schema check (item 4)
- Built a scratch database from `schema.sql` plus `mnemo migrate --to 006_lifecycle_edges`. Its `pg_dump --schema-only` matches production apart from **column order**:
  - Production added `atoms.remembered_on` with `ALTER TABLE`, so it is the last column.
  - `schema.sql` declares it mid-table.
  - This is harmless.
- Grants are identical.
- 006 is idempotent. Re-running it on the production copy only raised "already exists, skipping" notices.
- Dry run against production on 2026-11-21: pending `007_deactivation_tracking` and the index-drop migration (then numbered 008).
- Rehearsal on the copy: 007 deleted 318 self-loops (edges 44,455 → 44,137), and 008 dropped the index.
- The diff after 5a gets repeated against production itself.

### Tests
- Full suite against a fresh `mnemo_test` built with `schema.sql` plus `mnemo migrate`: **435 passed, 1 failed**, 17 eval tests deselected.
- The failure, `test_stats_enrichment.py::test_stats_includes_date_range`, is pre-existing and LLM-dependent. It fails on unmodified code too, and passes when run alone. The test stores "First memory stored today.", which Haiku sometimes decomposes into zero atoms.
- **Pre-existing problems (not fixed):**
  - `tests/test_client.py` imports `mnemo.client`, which moved out of the repo in `eda7ad3`, so the module fails to collect. Excluded with `--ignore`.
  - The suite loads `.env`, so the decomposer and lifecycle tests call the live Anthropic API using the production key.
- New tests:
  - `test_consolidation_defaults.py` (4)
  - `test_migrations.py` (8)
  - `test_phase0_small_fixes.py` (16)

### Regression fixtures (item 6)
Eight synthetic cases in `tests/fixtures/v2/`, run with `pytest -m eval tests/eval/test_v2_fixtures.py` at the Phase 0 dedup threshold of 0.97. Each was run 2–6 times against the live decomposer.

| case | today |
|---|---|
| attribution_addressee | pass |
| attribution_own_state | pass |
| abbreviation_verbatim | pass |
| negation_pair | pass |
| updated_number | pass at 0.97; **failed at 0.90**, where the corrected atom merged into the old one |
| read_your_writes | xfail (strict) |
| validity_window | xfail (strict) |
| planned_then_done | xfail (**non-strict**): fails about 1 run in 4, when lifecycle detection doesn't suppress the plan |

- The xfail `phase` is "phase TBD". `mnemo-v2-plan.md` isn't in the repo, so Tom needs to assign phases.
- The attribution cases pass when the speaker is labelled in the text. They guard against regressions but don't reproduce an unlabelled case.

---

## 5. Rehearsal on the production copy (preview, not the G2 numbers)
These numbers come from the dump taken 2026-11-21. The real G2 dry run runs against production in the data window.

### 5b classification
| deactivation_reason | source_type | atoms |
|---|---|---:|
| merged | consolidation | 23,430 |
| merged | direct_experience | 475 |
| merged | arc | 21 |
| arc | arc | 48 |
| decay_or_deleted | direct_experience | 6,677 |

The decayed and arc rows add up to 6,725. Merged arc atoms keep `merged` rather than `arc`.

### 5c restore preview: inactive `direct_experience` atoms, non-eval active agents
| agent | decayed (restore) | merged (stays inactive) |
|---|---:|---:|
| tapio | 679 | 3 |
| toms-claude-desktop | 554 | 373 |
| eaara | 499 | 10 |
| clio | 487 | 20 |
| illmarinen | 453 | 0 |
| nels-claude-desktop | 177 | 24 |
| amphion | 50 | 0 |
| inforge-researcher | 18 | 0 |
| nels-hermes-agent | 17 | 0 |
| lloyd | 15 | 0 |
| toms-claude-code | 4 | 0 |
| agent1 | 3 | 0 |
| **all** | **2,956** | **430** |

Restore totals:
- **Including** `nels-*` and `eaara`: 2,956.
- **Excluding** them: 2,263 (693 fewer).

### 5c-bis preview: active `consolidation` atoms, non-eval agents
| agent | atoms |
|---|---:|
| clio | 28 |
| tapio | 14 |
| illmarinen | 6 |
| toms-claude-desktop | 4 |
| nels-claude-desktop | 2 |
| inforge-researcher | 1 |
| eaara | 1 |
| **all** | **56** |

### 5e latency
- EXPLAIN ANALYZE on **production** (read-only, `ivfflat.probes=10`), clio's recall candidate query: the planner **does not use the ivfflat index**. It uses a bitmap scan on `idx_atoms_active` and a top-N sort, taking 9.7 ms. So dropping the index changes nothing for recall at today's sizes.
- On the copy, retrieve's candidate query (LIMIT 20) per agent took:
  - 0.6–11.6 ms with the index present.
  - 0.1–7.4 ms after 008 and VACUUM ANALYZE.
  - Every agent got the same row count both ways.
- This gets re-measured on production after 5c and 5d.

---

## Pre-item-5 decisions (Tom, 2026-11-22)
1. **`mnemo_eval`:** Tom ran the superuser restore (stdin redirect). Verified read-only:
   - 39,672 atoms, 44,455 edges, 25 agents and 29,638 `access_log` rows, matching the dump.
   - All 18 tables are owned by `postgres`.
   - Its `vector` extension is **0.8.7** (created fresh), while production's extension is at **0.8.2**. The host binaries are newer than production's installed extension version. Not changed.
2. **Migration role:** `scripts/phase0_create_migrator_role.sql` (commit `9b36a9d`). Tom runs it.
   - It creates LOGIN role `mnemo_migrator` (no superuser, createdb or createrole), with the password prompted by psql.
   - It transfers ownership of the non-extension tables, sequences, views and functions in `public` from `postgres` to `mnemo_migrator`.
   - Rehearsed twice on a restored copy: 18 relations and 2 functions moved, nothing left with `postgres`. The app role's grants are unchanged, including insert-only on `access_log`. `mnemo migrate` then applied 007 through 009 as `mnemo_migrator`.
3. **Fixture phases:**
   - `read_your_writes`: phase 1.
   - `validity_window`: phase 2.
   - `planned_then_done`: phase 2, strict.
   - `updated_number`: guard for phase 2 at any threshold. It now runs at 0.97 (passes) and at 0.90 (strict xfail, phase 2).
   - **Consequence:** as a strict xfail, `planned_then_done` passed (so reported FAILED) in 3 of 3 runs. The eval suite is red most of the time until phase 2, or until the case goes back to non-strict.
4. **Restore:** include `nels-*` and `eaara`. The approved total is 2,956.
5. **5c-bis:** approved. Deactivate the 56 active consolidation atoms (`deactivation_reason='consolidation_copy'`).
6. **Merged atoms:** the 430 merged `direct_experience` atoms stay inactive.
   - Their (kept_id, absorbed_id) pairs go into the new table `merge_pairs`, migration `008_merge_pairs`, holding ids and timestamps only.
   - Merge log rows store the kept atom in `access_log.target_id` and the absorbed atom in `metadata.absorbed_atom_id`.
   - **Renumbering:** the merge-pair table had to be applied before the index drop, so `008_drop_ivfflat_index` became `009_drop_ivfflat_index`. It had not been applied to production.

## Data-window runbook (`scripts/phase0/`)
Each execute script runs in one transaction and **rolls back unless the affected count equals the gate-approved `-v expected`**. All steps run as `mnemo_migrator` with the server stopped.

**Rehearsal** on a fresh restore of the 2026-11-21 dump, as `mnemo_migrator`:

| step | result |
|---|---|
| 5a `mnemo migrate --to 008_merge_pairs` | applied 007 and 008 |
| 5b classify | 0 unclassified; same split as the earlier rehearsal |
| 5c restore, `expected=1` | **rolled back** (guard test) |
| 5c restore, `expected=2956` | 2,956 restored |
| 5c-bis, `expected=56` | 56 deactivated |
| 5c-ter, `expected=430` | 430 pairs exported. 241 distinct kept atoms; for 378 of the 430 pairs the kept atom is active; no kept atom is missing |
| 5d production delete, 11 agents / 26,802 atoms | atoms 39,672 → 12,870; 0 non-eval agents changed atom count |
| 5d keep-only-eval (on a second copy) | 11 agents and 26,802 atoms left; 0 non-eval atoms |
| 5e `mnemo migrate`, VACUUM ANALYZE, latency | applied 009; vacuum OK |

**5e latency:** EXPLAIN ANALYZE per agent, after the restore, with no vector index. Each query is a bitmap heap scan on `idx_atoms_active` plus a sort.

| agent | rows | ms |
|---|---:|---:|
| eaara | 20 | 10.4 |
| clio | 20 | 7.7 |
| toms-claude-desktop | 20 | 7.6 |
| tapio | 20 | 5.8 |
| nels-claude-desktop | 20 | 3.5 |
| illmarinen | 20 | 2.5 |
| amphion | 20 | 1.2 |
| agent1 | 3 | 1.0 |
| inforge-researcher | 20 | 0.4 |
| lloyd | 20 | 0.3 |
| toms-claude-code | 20 | 0.2 |
| nels-hermes-agent | 20 | 0.2 |
| zethus | 2 | 0.0 |

## G3 candidate list (production, read-only, 2026-11-22)
- **Proposed for deletion:** the 11 `locomo-conv-*` agents (operator `tom`, created 2026-03-30/31), holding **26,802 atoms**. They have 0 capabilities, 0 views, 0 cross-agent edges, and 0 snapshot rows in other agents' views.
- **Cascade side effect:** **143 `agent_trust` rows** owned by 13 non-eval agents would be removed. Each of those agents trusts all 11 eval agents, apparently from automatic all-to-all trust at agent creation. 532 trust rows in total today.
- **Possible extra:**
  - `agent1`: operator `test`, created 2026-03-29, 3 atoms, never active.
  - `mimir`: operator `tom`, 0 atoms. Not benchmark data on its face.
- **Live drift since the baseline:** clio has grown 2,005 → 2,033 atoms. The old server is still running the hourly destructive consolidation, so the G2 counts will be re-run on production at the start of the window and checked against the approved numbers.

## Needed from Tom before the data window
1. **Run** `sudo -u postgres psql -d mnemo -v ON_ERROR_STOP=1 -f scripts/phase0_create_migrator_role.sql`, then add `MNEMO_MIGRATION_DATABASE_URL` to `.env`.
2. **G3:** confirm the eval list: the 11 `locomo-conv-*` agents, and whether `agent1` goes too. Confirm the 143 trust rows may cascade.
3. **Go-ahead** to stop the production server for the window (`docker compose stop mnemo-server`) and restart on `v2/phase0` afterwards.
4. **Whether `planned_then_done` stays strict** given the 3/3 XPASS.
