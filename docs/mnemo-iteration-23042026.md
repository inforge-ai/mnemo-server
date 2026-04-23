# Mnemo Iteration Tickets

**Date:** 2026-04-23
**Author:** Tom P. Davis
**Source:** Hermes dogfood feedback + SQL diagnostic session on `inforge-ops`

Four independent tickets, ordered by priority. Ticket 1 is exploratory and must complete before Ticket 4's value is measurable. Tickets 2 and 3 are independent of the others and can be worked in parallel.

---

## Ticket 1: Confidence update audit and recalibration

**Priority:** P0. Blocks meaningful use of `c_eff` in recall ranking.

### Background

Diagnostic queries on the production atoms table revealed that the confidence mechanism is alive but miscalibrated in three distinct ways. The composite recall ranking `similarity * (0.7 + 0.3 * c_eff)` is currently doing very little work because `c_eff` is either pinned at a single value for most atoms or saturated at ~1.0 for a small heavy-usage cohort.

### Findings

1. **Episodic and procedural atoms collapse to a single confidence ratio.**
   p25 = p50 = p75 = 0.889 across 4,627 episodic and 568 procedural atoms. That ratio is α/(α+β) = 8/9, meaning these atoms land at a fixed (α, β) after insert and never move. The ratio does not match the documented (2, 2) prior.

2. **Access correlates *negatively* with confidence.**
   `corr(c_eff, access_count) = -0.489`. Recall events appear to be penalising atoms rather than reinforcing them. Consistent with this: age-bucketed mean `c_eff` drifts down from 0.949 (< 1 day old) to 0.922 (> 30 days old) despite older atoms having ~38x more accesses on average. There is a sign error somewhere in the post-recall update path.

3. **Alpha grows without bound on a subset of semantic atoms.**
   `max(alpha) = 198,429`; `stddev(alpha) = 1,966`; `max(beta) = 1,001`; `stddev(beta) = 10.8`. A small set of semantic atoms — presumably dedup magnets that absorb near-duplicates at the 0.90 similarity threshold — are having their α bumped orders of magnitude more than β. Their `c_eff` saturates at ~1.0 and loses discriminative power.

4. **Beta can go below its prior.**
   `min(beta) = 1.0`, which is below the documented (2, 2) initialisation. Either the insert prior is not (2, 2) for all types, or there is a reset path that re-initialises β lower.

### Required investigation

Before writing code, produce a short written audit covering:

- **What the actual insert priors are per atom type.** Confirm the (α, β) values at insert for episodic, semantic, and procedural. Document any divergence from (2, 2).
- **What events update α and β, and with what magnitudes.** Enumerate every code path that mutates either parameter. For each, state the intended semantic (reinforcement, evidence against, dedup merge, etc.) and the actual arithmetic applied.
- **The origin of the negative access/confidence correlation.** Identify where access events decrement α or increment β. A swap in variable names, an inverted boolean, or a wrong sign on a delta are all plausible candidates.
- **The origin of β values below 2.0.** Either a lower initial prior for some types, or a reset path that re-initialises with a different prior. Locate and document.

### Required fixes

1. **Fix the access-update sign error.** Recall should either be neutral on confidence or weakly reinforce (bump α). Never penalise.

2. **Cap α growth on dedup-driven reinforcement.** Current behaviour lets a dedup magnet accumulate α into the hundreds of thousands, which crushes the top of the confidence distribution flat. Options, in order of preference:
   - Diminishing returns: scale the α increment by `1 / (1 + log(α + β))` or similar, so later merges contribute less than early ones.
   - Hard cap: clamp `α + β ≤ N` (suggest `N = 200` as a starting point) so the effective sample size is bounded.
   - Do not simply normalise α/(α+β) — we want to preserve the distinction between "seen many times with high agreement" and "seen a few times with high agreement", but the current unbounded growth is too aggressive.

3. **Fix the episodic/procedural collapse.** If these types are intentionally initialised at a different prior (e.g., (8, 1) for high-trust writes from the decomposer), document that. If they are supposed to move with usage, identify why they don't and fix. The current state where 27% of atoms are pinned at a single `c_eff` is not useful.

### Acceptance criteria

After fixes, a fresh diagnostic run against a test corpus (suggest seeding with the existing atom population and then running a scripted workload of stores + recalls) should show:

- `corr(c_eff, access_count)` ≥ 0 (ideally modestly positive, ≥ 0.1).
- No atom type has p25 = p75 unless that is documented as intentional.
- Histogram of `c_eff` shows mass spread across at least three of the top five bins (i.e., not crushed into a single bin).
- `max(alpha) / median(alpha)` ≤ 50 (versus current ~200,000 / 8 = 25,000).

### Non-goals for this ticket

- Do not change the composite ranking formula. The current `similarity * (0.7 + 0.3 * c_eff)` is fine once `c_eff` is working properly.
- Do not introduce new atom fields (lifecycle status, etc.). That is deliberately not on the roadmap.
- Do not alter the dedup threshold (0.90). If dedup is too aggressive, that is a separate ticket.

### Post-audit revisions (2026-04-23)

Phase 0 audit (`docs/mnemo-confidence-audit-23042026.md`) materially revises the above. Summary of what changed; the audit doc has the reasoning:

- **Required fix #1 (access-update sign error) is dropped.** No such bug exists. The recall path (`atom_service.py:732-739`) only bumps `last_accessed` and `access_count`; it does not touch α or β. The negative `corr(c_eff, access_count) = -0.489` reported in the v1 diagnostic was an artefact of two measurement errors (soft-deleted atoms included, raw Beta mean used instead of the server's decay-discounted `effective_confidence`). Active-only, server-c_eff correlation is −0.148 — weak and consistent with a cohort effect, not a sign bug.
- **Required fix #2 (cap α growth) stands.** `max(α) = 198,429` is still real. Preferred approach from the Review notes appendix unchanged.
- **Required fix #3 (episodic/procedural collapse) is reframed.** In server-space, episodic atoms land at mean c_eff ≈ 0.43 and procedural at ≈ 0.85, not at the raw-Beta-mean 0.889 the v1 diagnostic saw. The per-type disparity is the configured decay half-lives (14d / 90d / 180d) working as designed. Open questions moved to the audit doc: (a) is 14d episodic half-life correctly calibrated; (b) should the regex decomposer stop landing every default episodic at (8, 1). Neither is a fix; both are judgment calls.
- **"β below prior" investigation is retired.** The schema (2, 2) default is inert because every insert path supplies a value. β = 1 is a normal high-confidence landing.
- **Acceptance criteria need rewriting.** The four bullets in the original Ticket 1 were measured against raw α/(α+β); they are either already satisfied or no longer meaningful. Rewrite against `effective_confidence()` before Phase 1 begins. The one durable criterion: `max(alpha) / median(alpha) ≤ 50` (or similar) — that still measures the real problem.

Phase 1 Ticket 1 scope, post-audit: implement the α-growth fix (diminishing returns + safety ceiling per the Review notes), nothing else. Calibration questions (14d half-life; regex-decomposer landing) are separate tickets if we choose to pick them up.

---

## Ticket 2: Graph-aware recall (1-hop expansion)

**Priority:** P1. Independent of Ticket 1.

### Background

The atoms table currently has ~16,000 edges across ~19,000 atoms. Edges are created at write time but recall is pure vector similarity with no graph traversal. The edges are carrying zero signal to the caller.

During Hermes's dogfood session, a query about test-run costs failed to surface the connected atom identifying which project the test run belonged to (ABACAB), despite the two atoms being edge-linked. The caller had no way to traverse from the matched atom to its neighbours.

### Required change

After the initial vector-similarity top-k is selected, expand the result set by traversing 1-hop graph edges from each top-k atom. Include edge-connected atoms in the returned payload with a distinguishing annotation.

### Interface

Extend the `mnemo_recall` response to carry a `match_type` field on each returned atom:

- `match_type: "vector"` — matched directly by embedding similarity.
- `match_type: "graph"` — pulled in via 1-hop expansion from a vector match. Include `via: <uuid>` pointing to the vector-matched atom that was the expansion source.

The composite ranking score on graph-matched atoms should be computed as `source.score * edge_weight * 0.5` (the `0.5` is a discount factor to ensure vector matches generally outrank graph expansions unless the edge is very strong). Edge weight should come from the existing edge-confidence mechanism if one exists; otherwise use a constant 1.0.

### Acceptance criteria

- Querying for "test run costs" against the existing corpus surfaces at least one graph-expanded atom identifying the containing project (ABACAB), labelled `match_type: "graph"`.
- Graph-expanded atoms never outrank the vector match they expanded from.
- Top-k size (currently default 10) is preserved as the vector-match budget; graph expansion adds on top, up to a configurable ceiling (suggest default ceiling of `2 * k`).
- Deduplication: if a graph-expanded atom is already a vector match, emit once with `match_type: "vector"`.

### Non-goals

- Do not go beyond 1-hop. Deeper traversal can come later if justified.
- Do not change edge creation logic. This ticket is strictly about consumption of existing edges.

---

## Ticket 3: Decomposer entity validation

**Priority:** P1. Independent of Tickets 1 and 2.

### Background

During Hermes's dogfood session, cost-related atoms from an ABACAB test run were stored with text like `"In March 2026 test run, test tasks were cost black holes consuming 89% of total spend"` — no mention of ABACAB. The referential phrase "test run" was left unresolved. Retrieval could not distinguish these atoms from cost issues attached to other projects, because the project name was not in the atom text.

This is a write-time quality issue in the Haiku decomposer, not a retrieval-time issue.

### Required change

Extend the decomposer prompt and post-processing to validate that atoms do not contain unresolved referential noun phrases. Specifically:

1. When an atom text contains phrases like "the test run", "that meeting", "the project", "the system" (definite-article or demonstrative-plus-generic-noun patterns), check whether the referent is present in the same atom.

2. If the referent is present in the source context passed to the decomposer but not in the atom text, enrich the atom by substituting or appending the referent.

3. If the referent cannot be resolved from the source context, either (a) flag the atom as low-quality and store with a reduced initial confidence, or (b) reject the atom and log. Preference for (a) — preserves the signal, lets recall ranking de-prioritise.

### Implementation approach

A lightweight second pass in the decomposer is probably cleaner than a single mega-prompt. After initial atom generation, run a short validator prompt per atom with access to the same source context: "Does this atom contain all the named entities needed to understand it standalone? If a reference is unresolved, rewrite the atom with the reference filled in."

### Acceptance criteria

- Seeded test: decompose a paragraph mentioning "the ABACAB March 2026 test run had several issues: test tasks consumed 89% of spend, and cumulative diffs bloated verify costs." The resulting atoms should all mention ABACAB (or "ABACAB March 2026 test run") explicitly, not "the test run" or "test tasks".
- Recall over a corpus containing both ABACAB cost atoms and Sampo cost atoms, queried with "cost issues test run", should cleanly separate the two projects in the returned results.

### Non-goals

- Do not add structured entity tags as a separate field. The entity lives in the text. Keep it that way.
- Do not build a global entity graph or normalisation layer. Per-atom resolution against per-write source context is sufficient.

---

## Ticket 4: Decomposer type discrimination (episodic vs semantic)

**Priority:** P2. Depends on Ticket 1 being diagnosed (so we know what the real episodic prior is).

### Background

During Hermes's dogfood session, the atom `"Zulip integration is planned as a future pair-programming task"` was stored as semantic. It should have been episodic. When Zulip integration was later completed and a new atom stored, recall of "zulip integration" returned the two stale "planned" atoms (similarity 0.65 and 0.58) above the completion atom (0.54). The agent told the user Zulip integration was still planned, while talking to them over Zulip.

The underlying issue is that "X is planned" is not a timeless semantic claim. It is a temporally-scoped episodic claim with an implicit expiry. The decomposer is misclassifying claims of this shape.

### Required change

Tighten the type-assignment heuristic in the decomposer:

- **Semantic** atoms express timeless facts about the world: "Beancount uses double-entry accounting", "CPython's GIL serialises bytecode execution".
- **Episodic** atoms express things that happened, plans, states, or decisions at a point in time: "Tom decided to replace ERPNext with Beancount on 2026-02-15", "Zulip integration is planned (as of 2026-03-01)", "The BAM interview was completed on 2026-04-15".
- **Procedural** atoms express how to do something: "To deploy a new Hetzner VPS, run the inforge-ops Ansible playbook".

Any atom whose truth value depends on *when* the claim is made should be episodic and carry a `remembered_on` timestamp. This includes:

- "X is planned"
- "Y is the current Z"
- "W has not yet been done"
- Any forward-looking intention, plan, or schedule

### Behavioural consequence

Once episodic atoms are reliably tagged and timestamped, recall on a query like "zulip integration" that returns multiple episodic matches should rank by `remembered_on` recency within the episodic subset, so "Zulip completed" outranks "Zulip planned".

This is **not** a general contradiction-detection mechanism. Mnemo does not adjudicate whether two atoms contradict each other — that would compromise its role as a memory system (e.g., Newtonian gravity and Mercury's perihelion precession are technically contradictory but the contradiction is signal, not noise). This is specifically about recency-ranking *within* episodic matches on the same query, where each episodic atom carries its own timestamp.

### Acceptance criteria

- Seeded test: store "Zulip integration is planned" (episodic, remembered_on = T) and later "Zulip integration completed" (episodic, remembered_on = T+30days). Recall on "zulip integration" returns the completion atom first.
- Decomposer classification audit on a 100-atom sample: no atom tagged semantic is a forward-looking intention, plan, or "as of" state claim. Manual review.
- Semantic atoms remain semantic: timeless facts continue to be classified correctly.

### Non-goals

- Do not add a lifecycle state field (`active`, `superseded`, `archived`). Not needed if type discrimination is correct.
- Do not implement contradiction detection. Mnemo remains neutral on truth conflicts.
- Do not retroactively reclassify existing mis-typed atoms. Fix the writer going forward; the store will self-heal as new atoms supersede old ones by recency.

---

## Out of scope for this iteration

Two items from the original Hermes feedback are deliberately not included:

- **Summary / index atoms for broad exploratory queries.** "What are we working on today?" is a Sampo question, not a Mnemo question. Task state belongs in Sampo. Retrofitting Mnemo to answer it would pull us toward the document-store anti-pattern we are explicitly avoiding.
- **Shared views testing.** Zero shared views in production is a dogfooding gap, not a bug. Schedule a separate dogfood exercise with a cross-agent learning scenario (e.g., Ilmarinen sharing a procedural atom about a Hetzner quirk with Lloyd).

---

## Sequencing recommendation

Revised 2026-04-23 after review (see Review notes below). The work is split into three phases rather than run in parallel, because we are not attempting the full iteration in one sitting.

**Phase 0 — start immediately, in parallel:**

1. Ticket 1 *audit only*. Produce the written document described under "Required investigation": actual priors per type, every code path that mutates α or β, the origin of the negative access/confidence correlation, and the origin of β values below 2.0. No code changes yet. Expect 1–2 days of reading.
2. Ticket 4a — decomposer type-discrimination fix. Prompt and heuristic changes only; the acceptance criteria around classification (no forward-looking intentions tagged semantic, etc.) can be met without any Beta-distribution work. Split out from Ticket 4 on review; does not depend on Ticket 1.

**Phase 1 — once the Ticket 1 audit is delivered:**

3. Ticket 1 fixes (sign error, α growth cap, episodic/procedural prior collapse).
4. Ticket 2 — graph-aware recall. Independent; deferred from Phase 0 to avoid stacking streams.
5. Ticket 3 — decomposer entity validation. Independent; deferred from Phase 0 for the same reason.

**Phase 2 — once Ticket 1 fixes have landed:**

6. Ticket 4b — recency ranking within episodic matches. Behavioural consequence of Ticket 4a; landed after Ticket 1 so the ranking logic isn't built on a still-miscalibrated confidence channel, though the strict dependency is only on the presence of `remembered_on` on episodic atoms.
7. Re-run the confidence diagnostic suite against the Ticket 1 acceptance criteria.

---

## Review notes (2026-04-23)

A review pass raised four points. Recording them here with the disposition agreed in discussion; none materially changed the tickets, but two changed the sequencing and two sharpened acceptance criteria.

1. **α growth cap — hard cap vs diminishing returns.** Reviewer preferred the hard cap at `α + β ≤ 200` on debuggability grounds, arguing a system that has already gone off the rails benefits from the cruder, more predictable option. Counter-point: a hard cap silently drops further evidence once hit, including legitimate β updates. Agreed disposition: implement diminishing returns as the primary update law and add a safety ceiling (e.g. `α ≤ 1000`) as belt-and-braces. If the post-fix diagnostic still shows a saturated head, fall back to the hard cap.

2. **Graph discount factor as config.** Reviewer asked that the 0.5 discount in Ticket 2 be made configurable from day one rather than hard-coded. Agreed. Surface as `graph_recall.edge_discount` (or equivalent) in config; default 0.5.

3. **Split Ticket 4.** Reviewer observed that the decomposer type-classification change is a prompt/heuristic edit with no dependency on the Beta mechanics, and should not be gated on Ticket 1. The recency-ranking behavioural consequence is the part that benefits from Ticket 1 having landed. Agreed: split into 4a (classification, Phase 0) and 4b (recency ranking, Phase 2). See revised sequencing above.

4. **Ambiguous-case test for Ticket 3.** Reviewer asked for a test where the same generic noun could refer to different projects — e.g., "The deployment failed" with both an ABACAB and a Sampo deployment in the corpus. Agreed; add to Ticket 3 acceptance criteria as a follow-up when that ticket is picked up. Recording here rather than editing Ticket 3 inline so the original ticket prose stays as-drafted.
