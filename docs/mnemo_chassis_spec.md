# Mnemo Chassis Improvements — Claude Code Spec

**Purpose:** Implementation spec for Claude Code. Six targeted improvements based on design partner feedback. No new MCP tools — stays at 7. No embedding model changes.

**Repo:** `mnemo-server`
**Context:** A design partner (Claude Opus 4.6 via Claude Code) ran ~85 operations against the live API and produced a test report. These changes address the highest-impact findings. The decomposer, recall pipeline, and sharing protocol are all working well — this is finishing the chassis, not rebuilding the engine.

---

## Guiding Constraints

- **MCP tool count stays at 7.** No new tools. Changes are to REST endpoints, response formats, and server-side logic.
- **Token discipline.** Every byte added to recall output must earn its place. The 500-token default budget for `max_total_tokens` is already tight. Metadata additions must be compact.
- **No atom CRUD.** Mnemo uses belief revision, not document editing. To correct a memory, store the corrected version — the old atom's confidence decays through consolidation. This is by design. Do NOT add PUT/PATCH endpoints for atoms.
- **No new atom types.** The three-type taxonomy (episodic, semantic, procedural) is intentional and well-motivated. `relational` exists in the stats schema as a placeholder — do not add it to the decomposer.

---

## Task 1: Surface Confidence Metadata in Recall Output

**Problem:** The design partner repeated a fact 3 times and saw no visible confidence change. Confidence scores clustered at 0.78–0.91 with no meaningful differentiation. The composite score `similarity * (0.7 + 0.3 * c_eff)` compresses the confidence signal into a ~0.04 swing — invisible to the consuming agent.

**Root cause:** The recall response only returns the composite score. The raw Beta parameters (α, β) and effective confidence (c_eff) are computed but never surfaced.

**Fix:** At `verbosity=full`, append per-atom confidence metadata to the recall output.

**Current output format:**
```
[semantic] (high conf, 0.86) The Q1 target is $2M ARR.
```

**New output format (verbosity=full only):**
```
[semantic] (0.86, conf=0.83 α=5 β=1, stored=2026-03-20, hits=4) The Q1 target is $2M ARR.
```

**Fields:**
- `conf=` — effective confidence, i.e. `α / (α + β)`
- `α=` and `β=` — raw Beta distribution parameters
- `stored=` — atom `created_at` date (date only, not datetime)
- `hits=` — number of times this atom has been returned by recall (read from `access_log` count or a counter column)

**At `verbosity=summary` (the default):** No change. Keep the existing format. The metadata is only useful for debugging, development, and power users — not for routine agent consumption.

**Token budget impact:** ~15 tokens per atom at full verbosity. With 4–7 atoms typical, adds 60–105 tokens. Well within noise given the 500-token budget only governs atom *content*, not metadata framing.

**Implementation notes:**
- The recall endpoint already computes `c_eff` internally for the composite score — just include it in the response serialisation.
- `α` and `β` are on the atoms table (or computable from the confidence fields). Read them alongside the atom content.
- `created_at` is already on the atoms table.
- For `hits`: either add an `access_count` integer column to atoms (increment on recall), or count from `access_log`. A column is cheaper at query time. Add a migration to create it with default 0, and increment it in the recall code path after results are selected.

**Test:**
1. Store a fact: `"The project deadline is June 15."`
2. Store it again twice (identical or near-identical text).
3. Recall with `verbosity=full`.
4. Verify that α has incremented (should be > default prior).
5. Verify the metadata fields are present and correctly formatted.
6. Recall with `verbosity=summary` — verify output format is unchanged.

---

## Task 2: Verify and Fix Bayesian Update Code Path

**Problem:** It is unclear whether the dedup/merge path actually increments the Beta α parameter when a duplicate or near-duplicate atom is stored. The design partner's observation (confidence not visibly changing after 3 repetitions) could be a display issue (Task 1) OR a real bug where α is never incremented.

**Fix:** Audit the dedup merge path in the store/remember pipeline.

**Check these specific things:**
1. When a new atom is stored and the dedup check finds an existing atom above the 0.97 similarity threshold, does the code increment `α` on the existing atom?
2. If yes, by how much? (It should increment by 1 for each reinforcement — simple Bayesian counting.)
3. Is the updated α persisted to the database, or only computed in memory?
4. After incrementing α, is the composite score recomputed on next recall? (It should be — it reads α/β at query time.)

**If the code path IS working:** Task 1 alone solves the visibility problem. Document in a code comment that reinforcement increments α.

**If the code path is NOT working (α never increments):** Implement it:
```python
# In the dedup merge path (pseudocode):
existing_atom.alpha += 1  # Bayesian reinforcement
existing_atom.updated_at = now()
# Persist to DB
```

**Test:**
1. Store `"The sky is blue."` three times.
2. Query the atoms table directly: `SELECT alpha, beta FROM atoms WHERE text_content LIKE '%sky is blue%'`
3. Verify α = default_prior + 2 (two reinforcements beyond the initial store).

---

## Task 3: Mitigate the Super-Atom Problem

**Problem:** A generalised atom (produced by the consolidator merging diverse observations) appeared in 7 of 9 unrelated queries. Its embedding sits near the centroid of the memory space because it's semantically broad, so it scores moderately high against everything in gte-small's compressed similarity range.

**Fix:** Add a specificity penalty to the composite ranking for generalised/consolidated atoms.

**Current composite score:**
```
score = similarity * (0.7 + 0.3 * c_eff)
```

**New composite score:**
```
specificity = 1.0 if atom.source == 'decomposer' else 0.85  # 15% penalty for consolidated atoms
score = similarity * (0.7 + 0.3 * c_eff) * specificity
```

**Implementation:**
- The atoms table needs a way to distinguish decomposer-created atoms from consolidator-created atoms. Check if there's already a `source` or `origin` column. If not, add one: `source VARCHAR DEFAULT 'decomposer'` with allowed values `'decomposer'`, `'consolidator'`, `'direct'` (for atoms stored without decomposition).
- Apply the specificity multiplier in the recall ranking code, after the existing composite score computation.
- The 0.85 multiplier is a starting point. It means a consolidated atom needs ~15% higher raw similarity to rank equally with a decomposed atom. Tune based on testing.

**Why not just delete consolidated atoms?** Because they can be useful — they represent abstracted knowledge. The problem is that they're over-weighted in ranking because their broad embeddings score well against any query. The penalty corrects this without losing the knowledge.

**Test:**
1. Store 5 diverse facts across different topics.
2. Run consolidation (if it runs automatically) or trigger it.
3. Recall with a specific query related to only one of the 5 topics.
4. Verify the consolidated/generalised atom ranks below the specific on-topic atom.

**Migration:** Add `source` column if not present:
```sql
ALTER TABLE atoms ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'decomposer';
-- Backfill: mark any atoms known to be from consolidation
UPDATE atoms SET source = 'consolidator' WHERE [identify consolidator atoms — check for a flag, a null decomposer_version, or a specific pattern];
```

---

## Task 4: Enrich `mnemo_stats` with Cold-Start Summary

**Problem:** When an agent starts a new conversation, it has no orientation on what's in memory. The design partner flagged this as "no way to inventory or browse stored knowledge from a cold start."

**Fix:** Extend the existing `mnemo_stats` MCP tool response to include a compact summary section. **No new MCP tool.** All stats are already scoped per-agent (via `agent_id` parameter) — the summary fields must also be per-agent. Do NOT aggregate across agents or operators.

**Current stats response:**
```
Total memories: 632 (active: 632)
By type: {'episodic': 178, 'semantic': 341, 'procedural': 113}
Arc atoms: 29
Avg confidence: 89%
Edges: 8885
```

**New stats response:**
```
Total memories: 632 (active: 632)
By type: {'episodic': 178, 'semantic': 341, 'procedural': 113}
Arc atoms: 29
Avg confidence: 89%
Edges: 8885
Topics: mnemo architecture, FactSet hedging, Soliton newsletter, ABACAB agent, Sampo bus, information geometry
Date range: 2026-03-09 to 2026-03-24
Most accessed: "Q1 target is $2M ARR" (12 hits), "Mnemo deploys on Hetzner" (9 hits)
```

**New fields:**
- `Topics` — top N (5–8) topic clusters derived from domain_tags frequency, or failing that, from a lightweight clustering of atom texts. Domain tags are the cheap path — just count tag frequency and return the top N. If domain_tags coverage is sparse, fall back to the most common 2-grams or noun phrases from atom texts. Do NOT run an LLM call for this — it must be computable from the database alone.
- `Date range` — `MIN(created_at)` to `MAX(created_at)` across this agent's active atoms. Query must filter by `agent_id`.
- `Most accessed` — top 3 of this agent's atoms by `access_count` (from Task 1's new column), showing a truncated text snippet (first 60 chars) and hit count. If `access_count` doesn't exist yet, omit this field until Task 1 ships.

**Token impact:** Adds ~80–120 tokens to the stats response. This is a one-time call at conversation start, not repeated per recall, so the overhead is justified.

**Implementation:**
- All new fields are computed from existing database columns (domain_tags, created_at, access_count) with simple SQL aggregations, **filtered by agent_id** — same scoping as the existing stats query.
- The stats endpoint already hits the database scoped to one agent — extend the query, don't add a new endpoint.
- Cache the topic list and most-accessed list if performance is a concern at scale. Recompute on a schedule (hourly) or on stats request if atom count is below a threshold (e.g., <5000 atoms = compute live; ≥5000 = serve from cache).

**Test:**
1. Store 10 memories across 3 different domain_tags.
2. Recall 3 of them multiple times (to generate access_count data).
3. Call `mnemo_stats`.
4. Verify Topics shows the 3 domain tags.
5. Verify Date range spans the storage window.
6. Verify Most accessed shows the 3 frequently-recalled atoms.

---

## Task 5: Add Async Store Status Endpoint

**Problem:** `mnemo_remember` returns immediately with a `store_id` (async ack), but there's no way to check whether decomposition has completed. The design partner flagged this for production use — clients need to know when atoms are queryable.

**Fix:** Add a REST endpoint (NOT an MCP tool) for checking store status.

**Endpoint:**
```
GET /v1/stores/{store_id}/status
```

**Response:**
```json
{
    "store_id": "abc-123",
    "status": "complete",
    "atoms_created": 5,
    "created_at": "2026-03-24T10:30:00Z",
    "completed_at": "2026-03-24T10:30:02Z"
}
```

**Status values:**
- `pending` — store received, decomposition not yet started
- `decomposing` — Haiku decomposer is processing
- `complete` — atoms created and queryable
- `failed` — decomposition failed (include error in response)

**Implementation:**
- Check if there's already a `stores` or `store_jobs` table that tracks the async pipeline. If so, add a status column if not present. If not, create one:
  ```sql
  CREATE TABLE store_jobs (
      store_id UUID PRIMARY KEY,
      agent_id UUID NOT NULL REFERENCES agents(id),
      status VARCHAR NOT NULL DEFAULT 'pending',
      atoms_created INTEGER DEFAULT 0,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      completed_at TIMESTAMPTZ,
      error TEXT
  );
  ```
- The `mnemo_remember` handler creates the row with status `pending`.
- The async decomposer updates status to `decomposing` when it picks up the job, then `complete` (with atom count) or `failed` (with error) when done.
- Auth: require the operator's API key (same as other v1 endpoints). Scope: an operator can only query their own agents' store jobs.

**This is NOT an MCP tool.** It's for programmatic clients, benchmark harnesses, and monitoring. Agents don't need to poll — they just recall and get whatever's available. The tool count stays at 7.

**Test:**
1. Call `POST /v1/remember` with a multi-paragraph input.
2. Immediately call `GET /v1/stores/{store_id}/status` — expect `pending` or `decomposing`.
3. Wait 2–3 seconds, call again — expect `complete` with `atoms_created > 0`.
4. Verify a different operator's API key gets 404 (not 403 — don't leak existence).

---

## Task 6: Tighten Input Validation

**Problem:** Empty and whitespace-only inputs are silently accepted by `mnemo_remember`, creating empty atoms that waste storage and pollute recall.

**Fix:** Validate input at the API layer before it hits the decomposer.

**Validation rules for `POST /v1/remember` (and the `mnemo_remember` MCP tool):**

1. **Reject empty or whitespace-only text.** Return 422 with message: `"text must contain non-whitespace content"`
2. **Reject text shorter than 3 characters after stripping.** A 1-2 character memory is noise. Return 422: `"text must be at least 3 characters"`
3. **Reject text longer than 50,000 characters.** Return 413 with message: `"text exceeds maximum length of 50,000 characters. Split large documents into smaller sections before storing."` Rationale: 50K chars ≈ 12,500 tokens hitting the Haiku decomposer. Beyond this, API cost per call is unreasonable, decomposition quality degrades, and the caller should be chunking on their end. The current server accepts up to 5M characters — this is a vulnerability for both cost and abuse.
4. **Warn (don't reject) on text between 10,000 and 50,000 characters.** Accept but log a warning with the character count and agent_id. Track for cost monitoring and to inform whether the 50K ceiling needs adjusting.

**Implementation:**
- Add validation at the top of the remember endpoint handler, before the async job is queued.
- The MCP tool inherits this automatically since it calls the same endpoint.
- Check `len(text.strip())` for the minimum, `len(text)` for the maximum (don't strip before the max check — whitespace padding shouldn't be a way to sneak under the limit, but the raw input is what matters for cost).

**Test:**
1. `POST /v1/remember` with `text: ""` — expect 422.
2. `POST /v1/remember` with `text: "   \n\t  "` — expect 422.
3. `POST /v1/remember` with `text: "ab"` — expect 422.
4. `POST /v1/remember` with `text: "abc"` — expect 200/202 (accepted).
5. `POST /v1/remember` with 15,000 characters — expect 200/202 (accepted, warning logged).
6. `POST /v1/remember` with 50,001 characters — expect 413 (rejected).
7. `POST /v1/remember` with exactly 50,000 characters — expect 200/202 (accepted, warning logged).

---

## Implementation Order

1. **Task 6 — Input validation.** Quickest, no schema changes, prevents garbage from entering the pipeline while you work on the rest.
2. **Task 2 — Bayesian update audit.** This is a code read + possible small fix. Do it before Task 1 so you know what you're surfacing.
3. **Task 1 — Confidence metadata in recall.** Depends on Task 2 being correct. Includes the `access_count` column migration.
4. **Task 3 — Super-atom specificity penalty.** Requires `source` column migration. Independent of Tasks 1–2.
5. **Task 4 — Stats enrichment.** Depends on Task 1's `access_count` column for the "most accessed" field.
6. **Task 5 — Store status endpoint.** Independent. Can be done in any order but is lowest priority.

Each task is one commit.

---

## What This Spec Does NOT Cover

- **Embedding model swap.** That's a separate, larger decision driven by graph edge discrimination needs, not by this feedback.
- **Atom CRUD (update/patch).** Intentionally excluded — belief revision is the model.
- **Relational atom type.** Schema placeholder, not a feature.
- **New MCP tools.** Tool count stays at 7.
- **Composite score weight tuning (0.7/0.3 split).** Worth revisiting after Tasks 1–3 ship and you can see the real confidence distribution. Don't tune blind.
