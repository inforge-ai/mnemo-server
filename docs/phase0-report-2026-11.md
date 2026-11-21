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

**STOP: gate G1.** Waiting for Tom before starting the backup (item 2).
