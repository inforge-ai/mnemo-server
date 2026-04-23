# Mnemo Confidence Audit

**Date:** 2026-04-23
**Author:** Tom P. Davis (with Claude)
**Purpose:** Phase 0 deliverable for the iteration plan (`docs/mnemo-iteration-23042026.md`). Answers the four "Required investigation" questions in Ticket 1 and flags material revisions to the Ticket 1 "Required fixes" before Phase 1 commits to code.
**Scope:** All α/β write paths in `mnemo/server/` as of commit `17e5354`. Diagnostic queries run against `inforge-ops` / `mnemo` on the same date.

---

## TL;DR

The initial Ticket 1 diagnostic computed `c_eff` as raw α/(α+β) and included soft-deleted atoms. The server's ranking instead uses `effective_confidence()` (a SQL function combining raw Beta mean with a type-specific time-decay factor) and recall only considers active atoms. Re-running the diagnostic against the server's actual `c_eff` and filtering to `is_active = true` changes the picture materially:

- **Two of the three "Required fixes" have no target.** There is no sign error on recall (recall does not mutate α or β at all), and there is no (2, 2) prior that β is dropping below (the schema default is inert because every insert path supplies a value).
- **One fix remains real:** α can grow to ~198k on dedup magnets via the `+incoming_α − 1` Bayesian update. Decay dampens the downstream impact but the raw parameter is still pathological.
- **The "episodic/procedural collapse at 0.889" is a mis-read.** In server-space, episodic atoms land around 0.43 and procedural around 0.85. The per-type disparity is the configured decay half-lives (14d / 90d / 180d) doing their job. Whether those numbers are correctly calibrated is a separate question.

Recommendation: keep Ticket 1 on the roadmap but rescope it before Phase 1 begins. Concrete revisions proposed at the end of this document.

---

## 1. Actual insert priors per atom type

There is no single per-type prior. The `schema.sql` column default of `confidence_alpha = 2.0, confidence_beta = 2.0` is never actually used — every insert path supplies explicit values. The landing points are:

**Regex decomposer** (`mnemo/server/decomposer.py`, `_infer_confidence` at lines 180–193):

| Pattern | (α, β) | c_raw = α/(α+β) |
|---|---|---|
| `VERY_LOW_CONFIDENCE_PATTERNS` match | (2, 4) | 0.333 |
| `LOW_CONFIDENCE_PATTERNS` match | (2, 3) | 0.400 |
| `HIGH_CONFIDENCE_PATTERNS` match | (8, 1) | 0.889 |
| Atom classified as episodic (default) | (8, 1) | 0.889 |
| Semantic/procedural default | (4, 2) | 0.667 |

Arc atoms (`_maybe_create_arc` at line 242) are hard-coded to (4, 2).

**LLM decomposer** (`mnemo/server/llm_decomposer.py`, `_confidence_to_beta` at lines 66–85):

| Haiku-assigned confidence | (α, β) | c_raw |
|---|---|---|
| ≥ 0.80 | (8, 1) | 0.889 |
| ≥ 0.60 | (4, 2) | 0.667 |
| ≥ 0.40 | (3, 2) | 0.600 |
| ≥ 0.25 | (2, 3) | 0.400 |
| < 0.25 | (2, 4) | 0.333 |

**`store_explicit`** (`mnemo/server/services/atom_service.py:608–617`) maps power-user labels:

| Label | (α, β) |
|---|---|
| `high` | (8, 1) |
| `medium` | (4, 2) |
| `low` | (2, 3) |
| `uncertain` | (2, 4) |
| missing / unknown | (4, 2) |

**Implication for the audit's question:** β = 1 is a *normal high-confidence landing*, not a reset path pulling β below some prior. The schema's (2, 2) is not a prior used in practice. The audit question "why does β go below 2.0?" is malformed.

## 2. Every α/β mutation path

Five paths touch `confidence_alpha` / `confidence_beta`. Three of them write on insert (above) and two update existing atoms:

**`_merge_duplicate`** — `mnemo/server/services/atom_service.py:96–128`. Runs when a new incoming atom matches an existing active atom of the same type at cosine similarity > `duplicate_similarity_threshold` (default 0.90). The Bayesian-conjugate update:

```
new_α = max(1.0, existing_α + incoming_α − 1.0)
new_β = max(1.0, existing_β + incoming_β − 1.0)
```

Applied via `UPDATE atoms SET confidence_alpha = $1, confidence_beta = $2, last_accessed = now(), access_count = access_count + 1`. **The same event bumps access_count and confidence simultaneously.**

**`consolidate_near_duplicates`** — `mnemo/server/services/consolidation.py:290–348`. Offline consolidation job: finds pairs of active atoms of the same type with cosine similarity > 0.90 (same threshold as dedup), applies the same `α₁ + α₂ − 1` / `β₁ + β₂ − 1` update to the older atom, soft-deletes the newer one, and reassigns its edges. Runs outside the request path.

**Decomposer-local merge** — `mnemo/server/decomposer.py:204–222`, `_merge_adjacent`. Write-time merge applied to adjacent sentences of the same classified type within a single `/remember`, *before* storage. The arithmetic:

```
merged_α = max(prev.α, next.α)
merged_β = min(prev.β, next.β)
```

This is **not** the Bayesian update — it picks the more confident of the two by max/min. If a high-confidence episodic sentence (8, 1) is adjacent to a hedged one (2, 3), the merge produces (8, 1) and the hedge is erased. Only the regex decomposer calls this path; the LLM decomposer bypasses it.

**The recall path does not mutate α or β.** `atom_service.py:retrieve` executes `UPDATE atoms SET last_accessed = now(), access_count = access_count + 1` at lines 732–739 (and again at 769–776 for graph-expanded atoms). No confidence update. This is the direct answer to Ticket 1 "Required fix #1" — there is no access-update sign error because there is no access-update on confidence.

## 3. The negative access/confidence correlation

The initial diagnostic reported `corr(c_eff, access_count) = −0.489` and called it a smoking gun. It was not. Two measurement issues:

- **Soft-deleted atoms included.** The v1 query counted all 19,243 atoms regardless of `is_active`. Filtering to active atoms (`is_active = true`, n = 9,596) changes the raw correlation to **+0.038** — essentially zero.
- **Raw Beta mean, not server c_eff.** The v1 query computed α/(α+β) directly. The server ranks with `effective_confidence()` (schema.sql:283–323), which multiplies that base by a decay factor keyed on `now() − last_accessed`, plus a small access-count boost: `decay_factor = 0.5 ^ (age_days / (half_life × (1 + LN(1 + access_count) × 0.1)))`.

Measured against the server's actual `c_eff` on active atoms only (v2 diagnostic, 2026-04-23):

| Metric | v1 (all atoms, raw Beta) | v2 (active, raw Beta) | v2 (active, server c_eff) |
|---|---|---|---|
| `corr(c, access_count)` | −0.489 | +0.038 | **−0.148** |

The residual −0.148 on server c_eff is weak and consistent with a cohort effect, not a sign bug:

- Atoms in the 7–30 day age bucket (n = 7,515, 78% of active corpus) have mean_c_srv = 0.581 and mean_accesses = 66. Many are atoms that were queried heavily after creation and then not touched since — their `last_accessed` is stale, so decay bites, even though `access_count` is large.
- Atoms > 30 days old (n = 896) have mean_c_srv = 0.815 and mean_accesses = 81. These are continually-queried atoms that stay refreshed; their `last_accessed` is recent, decay is mild.
- Atoms < 1 day old have high c_srv (0.879) and low access_count, because age is trivially small.

**The mechanism is working roughly as designed.** The mild negative correlation in server-space reflects "atoms that were popular then stopped being queried decay" — which is what an access-weighted decay scheme is *supposed* to do.

**What this means for Ticket 1 fix #1:** No code fix required. The "recall should bump α" behaviour suggested by the ticket is an additional feature, not a bug fix. Whether to add reinforcement-on-recall is a design decision, not a correction — and if added, it needs care to avoid turning c_eff into a popularity score.

## 4. The origin of β values below the supposed prior

There is no supposed (2, 2) prior that β is dropping below. `min(β) = 1.0` is the clamp floor in both `_merge_duplicate` and `consolidate_near_duplicates`. Atoms with β = 1 originate at insert (high-confidence landings are (8, 1)) and stay there because the Bayesian update `β_new = β_existing + β_incoming − 1` returns 1 when both inputs are 1.

This is not a bug and does not need a fix. The audit question was based on a misreading of the schema default.

---

## 5. α growth on dedup magnets

This is the one confirmed pathology. From Q5 of the v1 diagnostic (unchanged by the v2 rerun, since these are raw parameters):

- max α = 198,429
- stddev α = 1,966
- max β = 1,001
- stddev β = 10.8

**Mechanism:** a small number of general-purpose semantic atoms sit at the centroid of a dense embedding region and match every incoming near-duplicate at similarity > 0.90. Each merge adds `(incoming_α − 1)` to existing α. For the typical high-confidence incoming of (8, 1), that is +7 per merge. To reach 198,429 from a starting (8, 1) requires on the order of 28,000 merges on one atom — plausible given a long-running agent with repetitive input.

**Why it matters in server-space:**
- Raw c_raw = 198,428 / 199,429 ≈ 0.9950. With decay, c_srv sits somewhere below depending on last_accessed, but the point is that this atom is indistinguishable from any other (α = 100, β = 1) atom once α is much larger than β. Further evidence stops moving the confidence meaningfully.
- In theory the atom is a crude summary that happens to be near every query in its semantic region. Its ranking contribution is roughly "always near the top of its zone", regardless of the actual specificity of the query.

**Fix is still warranted.** The Ticket 1 proposal (diminishing returns on the α increment, or a hard cap) stands. See the Review notes appendix of the iteration plan for the preferred approach (diminishing returns + safety ceiling).

## 6. The type-level c_srv disparity

From Q1v2:

| atom_type | n | mean c_raw | mean c_srv | p25 / p50 / p75 (c_srv) |
|---|---|---|---|---|
| episodic | 4,434 | 0.887 | **0.429** | 0.383 / 0.389 / 0.397 |
| procedural | 545 | 0.888 | **0.853** | 0.833 / 0.855 / 0.879 |
| semantic | 4,617 | 0.892 | **0.809** | 0.780 / 0.783 / 0.866 |

The episodic / procedural / semantic values are controlled by the decay half-lives in `mnemo/server/config.py:19–22`:

```python
decay_episodic: float = 14.0
decay_semantic: float = 90.0
decay_procedural: float = 180.0
decay_relational: float = 90.0
```

Episodic atoms are decayed 6.4× faster than semantic, 12.8× faster than procedural. That is deliberate — an observation is a time-bounded fact, a rule is a durable guideline — and the c_srv values reflect that design.

**The "collapse" observed in the v1 diagnostic was a collapse in the raw Beta mean, caused by the fixed (8, 1) landing for every default-episodic atom from the regex decomposer.** Once you apply the server's decay layer, the "collapse" turns into a tight-but-separated cluster per type, with each cluster sitting at a different level.

**Whether this is the right calibration is a separate question** — it's worth re-examining in light of Ticket 4 (episodic classification) and the separate matter of whether 14-day episodic half-life is too aggressive. But the original "27% of atoms pinned at one c_eff" framing is not what the recall ranker sees.

---

## Proposed revisions to Ticket 1 before Phase 1

Based on the above, recommended changes before writing code:

1. **Drop "Fix #1 — access-update sign error"** from the Required fixes list. No such bug exists. If the user still wants recall to weakly reinforce confidence (bump α on recall), file it as a separate design proposal, not a fix.

2. **Keep Fix #2 (cap α growth on dedup) as-is.** This is the one real finding. Preferred approach from the iteration plan's Review notes: diminishing-returns update law (`α += (incoming_α − 1) / (1 + LN(α + β))` or similar) plus a safety ceiling (e.g. `α ≤ 1000`) as backstop. If the post-fix diagnostic still shows a saturated head, fall back to a hard cap on `α + β`.

3. **Reframe "Fix #3 — episodic/procedural collapse"** as a calibration review. Two sub-questions that are genuinely open:
   - *Is the 14-day episodic half-life too aggressive?* Episodic facts that stay relevant (e.g. "Tom decided to replace ERPNext with Beancount") decay to c_srv ≈ 0.4 within a month, while procedural rules at half that age are still at 0.85+. Worth asking whether that gap matches how the memory system is meant to be used.
   - *Should the regex decomposer stop landing all episodic atoms at (8, 1)?* The LLM decomposer assigns confidence per-atom, so only the regex path has this behaviour. If the regex path is now a fallback used only on LLM errors, this may not be worth fixing; if it still gets real traffic, a more nuanced landing rule is warranted.

4. **Drop the "β below prior" investigation** (already satisfied here).

5. **Update Ticket 1 acceptance criteria** to measure against `effective_confidence()`, not raw α/(α+β). The v1 criteria (e.g. "histogram of c_eff shows mass spread across three of the top five bins") were written against the raw Beta mean and are either already satisfied or no longer meaningful once the decay layer is considered.

## Diagnostic artefacts

Scripts saved on the inforge-ops host:

- `/tmp/mnemo_confidence_diag.sql` — v1 diagnostic (raw Beta mean, all atoms).
- `/tmp/mnemo_confidence_diag_v2.sql` — v2 diagnostic (server `effective_confidence`, active atoms only, plus drift comparison query).

Both are idempotent read-only queries; re-running them later will give a fresh snapshot for comparison post-fix.
