# Mnemo v2 Phase 2: design note (gate G1)

**Date:** 2026-11-24. **Spec:** `docs/phase2-spec.md`. **Branch:** `v2/phase2` (mnemo-server, off `main` @ `927a27d`); mnemo-client `v2/phase2` (stacked on `v2/phase1`, which is PR #2 and still unmerged).

**Status:** awaiting Tom's review. No feature code has been written.

All production figures below are read-only counts taken on 2026-11-24. The note contains no atom or source text.

## 0. Inputs and what they imply

### Production shape (last 30 days unless noted)

| measure | value |
|---|---|
| completed stores | 69 (2.3 per day; nearly all clio, some lloyd) |
| atoms per store | mean 12.5, p50 11, p95 29, max 45 |
| active atoms | 9,050 (semantic 4,522, episodic 3,729, procedural 799) |
| active atoms with no `remembered_on` | 6,609 |
| agents with fewer than 5 active atoms | 2 of 13 |
| legacy edges | `supersedes` 187, `tension_with` 2,204, `narrows` 3,400, `related` 20,700, other 35 |
| `merge_pairs` | 430 |

Two counts differ from the spec: `supersedes` is 187 and `tension_with` is 2,204, against the spec's 152 and 2,197. Both grew after Phase 1. Backfill step 8.4 will use whatever the counts are when it runs.

There are 2,946 active atoms that still carry `deactivation_reason = 'decay_or_deleted'`. The status mapping (§1) derives status from `is_active` alone, so that leftover reason does not matter.

### OpenRouter Decisions API (docs read 2026-11-24)

Pages read:
- `docs/api-reference/decisions`
- `api-reference/alphadecisions/submit-a-decisions-request`
- `features/zdr`
- `features/provider-routing`
- `api_reference/limits`

What they say:
- **Request.** `POST https://openrouter.ai/api/alpha/decisions` with a Bearer key. The body is `{model, state, questions: {<key>: {type, instructions, criteria}}, provider?, session_id?, user?, trace?}`.
  - Several questions can share one `state` in a single request. No maximum number of questions is stated.
  - There is no endpoint for batching several items.
- **Question types.**
  - `choice`: `criteria` maps each option key to a description. The answer is `{choice, probabilities (sum 1), confidence}`.
  - `noul` (yes/no): `criteria {true, false}`. The answer is `{noul: 0–1, confidence}`.
  - A per-question `refusal` can come back while the other questions in the same request still resolve.
- **Usage.** The response's `usage.cost` gives the exact charge in credits.
- **ZDR.** The request schema's `provider` object includes `zdr` ("only endpoints that do not retain prompts will be used") and `allow_fallbacks`.
  - The docs give no specific error for "no ZDR endpoint available". They do say "a request an endpoint cannot serve fails with 400 before anything is sent upstream".
  - In the Phase 1 bake-off, `provider: {"zdr": true}` was accepted and routed to TypeSafe and Liquid respectively (`docs/phase1-bakeoff.md`).
- **Errors.** 400, 401, 402, 403, 404, 413, 429, 500, 502, 503, 524, 529.
  - A 402 with `limit_source = openrouter_in_flight_budget` is transient. Honour `Retry-After`.
  - No rate limit is documented for Decisions.
  - No timeout is documented.
- **Not published:** per-model pricing and context length for `jev-1.14` and `d1.2-ultra` (their model pages return 404). The estimates here use the bake-off's measured `usage.cost`:
  - Jev: about $0.018–0.021 per 1,000 decisions, p50 0.17–0.18 s, p95 0.20–0.21 s at concurrency 8.
  - Liquid: about $0.007–0.008 per 1,000, p50 0.20–0.22 s.

## 1. Migration `011_relations.sql`

```sql
-- atoms
ALTER TABLE atoms
  ADD COLUMN kind           text NOT NULL DEFAULT 'unknown'
        CHECK (kind IN ('state','plan','observation','rule','unknown')),
  ADD COLUMN kind_p         double precision CHECK (kind_p BETWEEN 0 AND 1),
  ADD COLUMN status         text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','superseded','merged','archived')),
  ADD COLUMN valid_from     timestamptz,
  ADD COLUMN valid_until    timestamptz,
  ADD COLUMN date_precision text NOT NULL DEFAULT 'unknown'
        CHECK (date_precision IN ('day','month','year','unknown'));
UPDATE atoms SET status = 'archived' WHERE NOT is_active;          -- one pass, ~3.9k rows
ALTER TABLE atoms ADD CONSTRAINT atoms_status_is_active
  CHECK ((status = 'archived') = (NOT is_active));
CREATE INDEX idx_atoms_agent_status ON atoms (agent_id, status) WHERE is_active;

-- keep status in step with is_active (66 code sites write is_active; none change)
CREATE FUNCTION atoms_sync_status() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.is_active IS DISTINCT FROM OLD.is_active THEN
    NEW.status := CASE WHEN NEW.is_active THEN 'active' ELSE 'archived' END;
  ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
    NEW.is_active := (NEW.status <> 'archived');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER atoms_sync_status BEFORE UPDATE OF is_active, status ON atoms
  FOR EACH ROW EXECUTE FUNCTION atoms_sync_status();

-- edges
ALTER TABLE edges
  ADD COLUMN decided_by text CHECK (decided_by IN
        ('legacy_lifecycle','relation_v2','relation_v2_backfill','agent')),
  ADD COLUMN retired_at timestamptz;
UPDATE edges SET decided_by = 'legacy_lifecycle'
  WHERE edge_type IN ('supersedes','tension_with','narrows');     -- ~5.8k rows
ALTER TABLE edges DROP CONSTRAINT edges_edge_type_check, ADD CONSTRAINT edges_edge_type_check
  CHECK (edge_type IN ('supports','contradicts','depends_on','generalises','specialises','motivated_by',
                       'evidence_for','supersedes','summarises','related','tension_with','narrows',
                       'same','replaces','refines','conflict','review'));

-- one row per pair asked
CREATE TABLE relation_decisions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id       uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  new_atom_id    uuid REFERENCES atoms(id) ON DELETE CASCADE,  -- NULL only in shadow when legacy dedup merged the new atom
  old_atom_id    uuid NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  source_id      uuid,                                          -- the new atom's note
  model          text,                                          -- NULL while pending
  probabilities  jsonb,       -- {"relation":{...}, "change":p, "same_attribute":p, "correction":p}
  inputs         jsonb NOT NULL,  -- kinds, kind_p, valid_from, precision, origin_class, time_unresolved, cosine (both sides)
  raw_choice     text,
  final_relation text NOT NULL CHECK (final_relation IN
        ('same','replaces','refines','conflict','unrelated','review','pending')),
  guard_fired    text,
  mode           text NOT NULL CHECK (mode IN ('shadow','enforce')),
  decided_by     text NOT NULL CHECK (decided_by IN ('relation_v2','relation_v2_backfill')),
  edge_id        uuid REFERENCES edges(id) ON DELETE SET NULL, -- enforce only
  attempts       int NOT NULL DEFAULT 1,
  latency_ms     int,
  cost           numeric,
  created_at     timestamptz NOT NULL DEFAULT now(),
  decided_at     timestamptz
);
CREATE INDEX idx_rd_new ON relation_decisions (new_atom_id);
CREATE INDEX idx_rd_old ON relation_decisions (old_atom_id);
CREATE INDEX idx_rd_pending ON relation_decisions (created_at) WHERE final_relation = 'pending';

CREATE TABLE conflicts (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id            uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  atom_a              uuid NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  atom_b              uuid NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  decision_id         uuid REFERENCES relation_decisions(id) ON DELETE SET NULL,
  opened_at           timestamptz NOT NULL DEFAULT now(),
  resolved_by_atom_id uuid REFERENCES atoms(id) ON DELETE SET NULL,
  resolved_at         timestamptz,
  CHECK (atom_a <> atom_b)
);
CREATE UNIQUE INDEX uq_conflict_open ON conflicts (LEAST(atom_a, atom_b), GREATEST(atom_a, atom_b))
  WHERE resolved_at IS NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON relation_decisions, conflicts TO mnemo;
INSERT INTO schema_migrations (version) VALUES ('011_relations');
```

- **`status` vs `is_active`.** `archived` means the same as `is_active = false`: forgotten, decayed, legacy-merged, or an arc. `superseded` and `merged` keep `is_active = true`, so whether they are hidden is controlled only by `recall_hide_superseded`.
  - The trigger and the CHECK keep the two columns consistent without touching the 66 code sites that read or write `is_active`.
  - Only v2 and the supersede API set `superseded` or `merged`, through `relation_service.apply()`.
- **Which existing edges get `decided_by`.** Only the lifecycle types (`supersedes`, `tension_with`, `narrows`) get `legacy_lifecycle`. Structural edges such as `related` and `summarises` stay NULL.
  - The spec says existing rows get `legacy_lifecycle`. I read that as the lifecycle edges, since a `related` similarity edge was never a lifecycle decision. **Tom to confirm.**
- **New edge types** mirror the final relations: `same`, `replaces`, `refines`, `conflict`, `review`. Legacy `supersedes` edges stay as they are and never set status.
- **Migration runtime.** The column adds are metadata-only on PG16 (constant defaults). The two UPDATEs touch about 3.9k atoms and 5.8k edges, which takes seconds.
- **Dry run.** As in Phase 1, `mnemo migrate --dry-run` runs against production first.

## 2. Decision-model questions (exact text)

There are two request shapes, so every pair or atom costs one round trip. The questions in a request share its `state`.

### 2.1 Per-atom request (at write time, once per new atom)

`state`:
```
CONTEXT: written by {speaker}{" to " + addressee if addressee}; note dated {remembered_on as YYYY-MM-DD or "undated"}; stored {created_at as YYYY-MM-DD}.
QUOTE:
{source_quote}
ATOM:
{atom text}
EVENT DATE: {event_date as YYYY-MM-DD, or "none"} (precision: {date_precision})
```

**`kind`** (`choice`). Instructions: "What kind of statement is the ATOM? Judge the ATOM itself. The CONTEXT only helps with names and dates."

| key | criterion |
|---|---|
| `state` | A property or situation that holds from some time until it changes: a status, a current value or setting, a baseline, where something is, what something uses. |
| `plan` | An intention, booking, schedule or expectation about something that has not happened yet. |
| `observation` | Something that happened or was measured at a particular time: an event, an action taken, a single reading. |
| `rule` | A general fact, preference, procedure or definition that is not tied to a particular time. |

**`grounded`** (`noul`). This is the Phase 1 bake-off grounding question, verbatim.
- Instructions: "Is the ATOM fully supported by the QUOTE? Answer true only if every claim in the ATOM (numbers, names, abbreviations, who did what, negation) is stated or directly implied by the QUOTE. The CONTEXT says who wrote the quote and when; the ATOM may use it for names and dates."
- Criteria: `true` "The quote fully supports the atom."; `false` "The atom adds, changes or contradicts something in the quote."

**`time_unresolved`** (`noul`). This is asked only when the deterministic prefilter hits. The prefilter is the wide relative-time list from the 2026-11-24 count, matched in the atom text, together with no explicit day-and-month date in the text and `EVENT DATE` none.
- Instructions: "Does the ATOM refer to a time relative to a date it does not state (for example 'tomorrow', 'last week', 'on Monday'), so that the actual date cannot be worked out from the ATOM, its EVENT DATE and the note date? Words such as 'now' or 'currently' that only say something is presently true do not count."
- Criteria: `true` "The atom depends on a reference date it does not give."; `false` "The atom's time is stated or can be worked out, or the words only mark a current state."

### 2.2 Per-pair request (new atom × each of the k candidates)

`state`:
```
EXISTING ATOM (kind {kind}; event date {valid_from as YYYY-MM-DD, or "none"}, precision {date_precision}; origin {origin_class}):
{old text}

NEW ATOM (kind {kind}; event date …, precision …; origin …):
{new text}
```

A kind shows as `unknown` until it is known (§4). The model sees kinds and dates because the bake-off showed that context sharpens the replace/refine split. Whatever the model says, the matrix still decides.

**`relation`** (`choice`). Instructions: "How does the NEW ATOM relate to the EXISTING ATOM?"

| key | criterion |
|---|---|
| `same` | Both state the same fact about the same thing for the same time; the new atom adds nothing and changes nothing. |
| `replaces` | The new atom makes the existing one out of date: a newer value of the same property, a changed state, a plan that happened, was cancelled or was moved, or an explicit correction. The existing atom was true or believed earlier and is no longer current. |
| `refines` | The new atom qualifies, scopes or adds detail to the existing one without making it false. |
| `conflict` | The atoms make incompatible claims about the same thing for the same time, and neither says it corrects the other. |
| `unrelated` | They mention overlapping people, systems or topics but state independent facts. Two readings or events on different days are independent of each other. |

**`change`** (`noul`, ‡). Instructions: "Does the NEW ATOM report that the situation in the EXISTING ATOM has changed (the state ended or took a new value), or that the planned thing happened, was cancelled or was moved? A reading of a different quantity, or another event of the same kind, is not a change." Criteria: `true` "It reports that change."; `false` "It does not."

**`same_attribute`** (`noul`, †). Instructions: "Are both atoms about the same property of the same subject (for example both give one person's resting heart-rate baseline, or both say which database one service uses)?" Criteria: `true` "Same subject, same property."; `false` "A different subject or a different property."

**`correction`** (`noul`, §). Instructions: "Do both atoms describe the same single event or measurement, with the NEW ATOM correcting a detail of it?" Criteria: `true` "Same event; the new atom corrects it."; `false` "Different events, or no correction."

### 2.3 Wording and order

The bake-off found that option order barely matters (0–1.5% of labels flip), while rewording matters more (4–10.5% flips on relations).
- So the wording above is fixed and versioned (`QUESTIONS_VERSION = "q1"`, recorded in `relation_decisions.inputs`).
- The kind bake-off (§4) runs it in three variants: base, reordered, reworded. If the reworded variant flips more than 10% of labels, the wording is reported before any threshold is set.

## 3. The matrix and global guards as code

This is a new module, `mnemo/server/services/relations.py`. It is pure, with no I/O.

```python
KINDS = ("state", "plan", "observation", "rule")
# cell -> {relation: condition}; condition None = no extra condition
MATRIX = {
  ("state","state"):             {"replaces": "dagger", "refines": None, "conflict": None},
  ("state","plan"):              {"replaces": "ddagger", "refines": None},
  ("state","observation"):       {"refines": None},
  ("state","rule"):              {"refines": None, "conflict": None},
  ("plan","state"):              {"refines": None},
  ("plan","plan"):               {"replaces": "dagger", "refines": None},
  ("plan","observation"):        {"refines": None},
  ("plan","rule"):               {"refines": None},
  ("observation","state"):       {"replaces": "ddagger", "refines": None, "conflict": None},
  ("observation","plan"):        {"replaces": "ddagger", "refines": None},
  ("observation","observation"): {"replaces": "section", "refines": None, "conflict": None},
  ("observation","rule"):        {"refines": None, "conflict": None},
  ("rule","state"):              {"refines": None},
  ("rule","plan"):               {"refines": None},
  ("rule","observation"):        {"refines": None},
  ("rule","rule"):               {"replaces": "dagger", "refines": None, "conflict": None},
}
# `same` is allowed wherever new.kind == old.kind (and both known; see open question 4).
# Any cell with an `unknown` side allows only {"refines": None}.

def decide(new: Side, old: Side, ans: Answers, th: Thresholds) -> Decision:
    """Side: kind, valid_from, precision, origin_class, time_unresolved.
    Answers: relation probs + change / same_attribute / correction p.
    Returns Decision(final, guard_fired, raw)."""
    raw = argmax(ans.relation)
    if raw == "unrelated":
        return Decision("unrelated", None, raw)
    allowed = cell(new.kind, old.kind)                      # includes `same` rule
    if raw not in allowed:
        return downgrade(ans, th, raw, guard="matrix")
    cond = allowed[raw]
    if cond == "dagger" and not (ans.same_attribute >= th.attr and new.origin_class == old.origin_class):
        return downgrade(ans, th, raw, guard="dagger")
    if cond == "ddagger" and ans.change < th.change:
        return downgrade(ans, th, raw, guard="ddagger")
    if cond == "section" and not (new.precision == old.precision == "day"
                                  and same_day(new.valid_from, old.valid_from)
                                  and ans.correction >= th.correction):
        return downgrade(ans, th, raw, guard="section")
    if ans.relation[raw] < th[raw]:                         # per-relation probability threshold
        return downgrade(ans, th, raw, guard="threshold")
    if raw == "replaces":
        if new.origin_class != old.origin_class:
            return Decision("conflict", "origin_differs", raw)
        if new.precision not in ("day", "month") or old.precision not in ("day", "month"):
            return Decision("review", "precision", raw)
        if new.time_unresolved or old.time_unresolved:
            return Decision("review", "time_unresolved", raw)
        if earlier_at_coarser(new.valid_from, old.valid_from, coarser(new.precision, old.precision)):
            return Decision("review", "date_order", raw)
    return Decision(raw, None, raw)

def downgrade(ans, th, raw, guard):
    return Decision("refines" if ans.relation["refines"] >= th.refines else "unrelated", guard, raw)
```

**Order of checks.** The matrix and its conditions run first, then the probability threshold, then the global guards. Two of the four guards were simplified:
- The spec's "origin classes differ **and the atoms disagree**" guard: `raw == "replaces"` already means the model judged that they disagree, so that part is implied.
- The spec's "model probability ≥ tuned threshold" guard is the threshold check above.

**What happens when a guard fails:**
- origin differs → `conflict`;
- precision, unresolved time or date order → `review`;
- probability threshold → downgrade.

**Undated atoms.** If `valid_from` is NULL on either side, precision is `unknown`, so the precision guard turns any `replaces` into `review`.

**Tests.** `tests/test_relations_matrix.py`, table-driven:
- each of the 16 cells × each of the 5 raw choices;
- †, ‡ and § each passing and failing;
- each guard, including the origin case turning into `conflict` and the coarser-precision date compare (a `month` atom against a `day` atom in the same month counts as not earlier);
- `unknown` kinds;
- the downgrade split at the refines threshold.

**Starting thresholds** (shadow only; tuned at G3, §6):
- relation probabilities: `replaces` 0.80, `same` 0.85, `conflict` 0.60, `refines` 0.50;
- yes/no questions: `change` 0.50, `same_attribute` 0.50, `correction` 0.50;
- kind 0.60.

Jev's yes/no probabilities are known to be harsh: its grounding question tuned to about 0.09 in the bake-off. So the three yes/no thresholds are expected to move, and shadow records the raw probabilities.

## 4. Kind classification and dates at write time

- **Decomposer prompt v3** (`prompt_v3`) adds two fields per atom:
  - `event_date`: ISO, or null.
  - `date_precision`: `day`, `month`, `year` or `unknown`.

  The decomposer still does not decide kind. `decomposer_version` becomes `anthropic:claude-haiku-5-5:prompt_v3`.
- **`valid_from` / `date_precision` at write time:**

  | kind | `valid_from` | `date_precision` |
  |---|---|---|
  | state, rule, observation | `event_date`, else `remembered_on` | from the decomposer, or `day` if a caller supplied `remembered_on` |
  | plan | when the plan was made: `remembered_on`, else the store time | (see below) |
  | all, when nothing is given | the store time (`created_at`) | `day` |

  For plans, the scheduled date stays in `event_date` inside `structured`.

  The fallback to the store time is a proposal: the store time is a true "as of" date for a state or a plan. **Open question 3:** the alternative is NULL / `unknown`, which blocks every automatic `replaces` for undated notes.
- **Kind below its threshold** is stored as `unknown`. Such an atom can be linked, but it is never replaced and never replaces anything.
- **Candidates with no kind yet.** Before backfill step 8.3, nearly every candidate is `unknown`, so shadow would show almost no `replaces`. Proposal: when a candidate's kind is `unknown`, the pair step first asks the per-atom `kind` question for that candidate, which is one extra request.
  - In **shadow**, that answer goes only in `relation_decisions.inputs`; atoms are not touched.
  - In **enforce**, it is also written to `atoms.kind` / `kind_p`, so each old atom is classified at most once.
  - Backfill 8.3 then only covers what is left.
- **Candidate precision for haiku_v1 atoms** is derived on the fly by the same deterministic rule as backfill 8.1 (`effective_precision(atom)`) until that backfill has stored it.
- **Kind bake-off (STOP before choosing):**
  - Data: about 150 synthetic atoms, balanced across the four kinds and written by Haiku 5.5 through Anthropic, with invented names. It includes the hard boundaries: a baseline as a state versus a reading as an observation, a booked appointment as a plan, and a habit or preference as a rule.
  - Models: Jev, d1.2-ultra, and Haiku 5.5 as a generative baseline.
  - Variants: base, reordered and reworded.
  - Reported: accuracy, per-class recall, a reliability table, flips, p50/p95 latency, and $ per 1,000.
  - The run extends `scripts/eval/decision_bakeoff.py` as a new task C. Synthetic text only, so no production text goes to any provider.

## 5. Store path, latency and failure

```
route ── create_store (tx1: store_jobs + sources)                      unchanged
      ── prepare_atoms: decompose (v3) + embed + deterministic checks   unchanged, no conn
      ── fetch_candidates: ONE short conn; k=5 nearest active atoms per
         new atom (same agent, status active, other notes only), plus
         those candidates' kind / dates / origin                        new
      ── decide (no conn; budget = 3 s from the start of this step):
           per-atom requests  (n atoms; +1 per unknown candidate kind)
           per-pair requests  (n × k), all concurrent, semaphore
           MNEMO_RELATION_CONCURRENCY (default 16), each request
           timeout 1.5 s; retry ×2 on 429/5xx/524/529/in-flight-402
           (honour Retry-After within the budget), then the fallback
           model, then give up → pair `pending`                          new
      ── persist (tx2): insert atoms (kind, kind_p, valid_from, precision,
           verification incl. grounded p), relation_decisions rows,
           and in enforce: apply() edges / status / valid_until /
           conflicts / evidence, re-checking each old atom is still
           `active` (SELECT … FOR UPDATE; if not → final `review`,
           guard `stale`)                                               extended
      └─ background: pending-pair worker; legacy lifecycle detector
         (shadow until G3)                                              new / unchanged
```

- **Requests per store.** With n atoms and k = 5, a store makes n + 5n requests, plus up to 5n kind requests for unknown candidates.
  - At p50 (n = 11): 66 requests, up to about 120.
  - At p95 (n = 29): 174 requests, up to about 320.
- **Latency estimate.** At concurrency 16 and Jev's measured p50 of 0.18 s: about 0.8 s at p50 and about 2.5–4 s at p95, so the 3 s budget will run out on the largest stores. Whatever is unfinished is recorded `pending`, listed under `relations_pending`, and finished by the worker.
- **What gets reported:** p50 and p95 of `remember` with and without the check, and the share of stores that run out of budget. Synthetic first, on the test DB; then from production timings during shadow.
- **Fail open.** If the decide step fails for any reason, including an exception, missing key or outage, every pair is `pending`, the atoms are stored, and the response is 201. The decision client never raises into the store path; this is unit-tested.
- **The pending worker** is a periodic loop, like the consolidation scheduler, every 2 minutes.
  - It picks the oldest `pending` rows with `attempts < 6`, using exponential backoff by attempt, and decides them.
  - In enforce mode it applies the result in a short transaction.
  - After 6 attempts a row stays `pending` and is counted in the report.
- **Dedup.** In **shadow**, the 0.97 cosine merge keeps running, so the store behaves as it does today.
  - When it merges a candidate, the v2 decision for that atom is still recorded, with `new_atom_id` NULL and the dedup target listed in `inputs`. That lets shadow compare `same` with the old merge.
  - In **enforce**, the cosine merge is off and `same` replaces it.
- **Legacy detector.** It keeps running until G3, writing only edges with `decided_by = 'legacy_lifecycle'`. Its candidate band and thresholds stay as they are.
- **Recall hiding.** `_filter_superseded` and the `recent` SQL hide by `status IN ('superseded','merged')` instead of legacy `supersedes` edges. This matters only when `recall_hide_superseded = true`, which comes at G3.
- **Settings:**
  - `MNEMO_RELATION_MODE`: `off`, `shadow` or `enforce`. Default `off`; production goes to `shadow` at G2.
  - `MNEMO_RELATION_K`: default 5.
  - `MNEMO_RELATION_BUDGET_SECONDS`: default 3.0.
  - `MNEMO_RELATION_CONCURRENCY`: default 16.
  - `MNEMO_DECISION_MODEL`: default `typesafe/jev-1.14`.
  - `MNEMO_DECISION_FALLBACK_MODEL`: default `liquid/d1.2-ultra`.
  - One setting per threshold.
  - `MNEMO_OPENROUTER_API_KEY` goes in `.env` for the first time. **Open question 1** covers the data rule; the key is never committed.

### Decision client (`mnemo/server/decisions/`)

`DecisionModel` is a protocol with one method: `ask(state, questions) -> Answers | None`. There are two implementations: `OpenRouterDecisions` and `FakeDecisions` (scripted, for tests).

- **Request body.** Every request body is built by one function, `_body()`, which always sets `provider = {"zdr": true, "data_collection": "deny"}`. A final assertion refuses to send any body without it.
  - A test sends through a mock transport and checks every captured body, including retries and the fallback model.
  - A second test proves that a body without `zdr` is never sent.
- **No connection held.** Calls never hold a DB connection: the client takes no connection argument, and the store path calls it between the two transactions.
- **Recorded per request:** model, probabilities, `usage.cost` and latency, in `relation_decisions` and in per-atom `verification`.
- **Logs** carry ids, counts and timings only.

## 6. Shadow review and switch-on (G2 → G3)

**`scripts/phase2/review_shadow.sql`.** Tom runs it himself, like the Phase 1 supersedes review.
- It prints each pair whose final relation is `replaces`, `same` or `conflict`, plus a random 20 of the rest (`-v seed`).
- For each pair it shows both texts, kinds, `valid_from`, precisions, origin classes, raw and final relation, the guard that fired, and all probabilities.
- It then uses `\copy` to write a verdict template (decision id, final relation, an empty `verdict` column) to `~/mnemo-review/shadow_verdicts_<date>.csv`, outside the repo. Tom fills in `verdict` as `ok` or a corrected relation.

**`scripts/phase2/review_phase1_pairs.sql`** prints, for Tom's 15 judged Phase 1 pairs, the legacy edge next to v2's decision. The pairs are re-decided in shadow by `scripts/phase2/redecide_pairs.py --edge-ids …`, which runs in the container and writes `relation_decisions` only.

**Shadow report, counts only.** It runs after at least 7 days or 100 decided pairs, whichever comes later.
- decisions by final relation and by guard;
- agreement with the legacy detector on the same pairs, by legacy type;
- probability histograms per relation;
- pending count and attempts;
- p50/p95 `remember` with and without the check;
- cost per 1,000 stores.

**G3 tuning.** For each threshold, the report shows the wrong-`replaces` and wrong-`same` counts, and the right decisions lost, across a grid from 0.5 to 0.95. The aim is zero wrong at the smallest loss. Then Tom decides on:
- `MNEMO_RELATION_MODE=enforce`;
- `MNEMO_LIFECYCLE_DETECTION_ENABLED=false`;
- `MNEMO_RECALL_HIDE_SUPERSEDED=true`.

## 7. API and MCP

**Server endpoints** (agent key only; the owning agent only):

| method and path | body / params | does |
|---|---|---|
| `POST /v1/agents/{id}/atoms/{atom_id}/supersede` | `{by_atom_id?: uuid, text?: str, reason: str}` (exactly one of `by_atom_id` / `text`) | Writes a `replaces` edge with `decided_by = 'agent'`; the old atom becomes `superseded` with `valid_until`. With `text`, the new atom is stored through `remember`, with `atom_id` forced as its target, so the relation step is skipped for that pair. |
| `POST /v1/edges/{edge_id}/undo` | `{reason?: str}` | **`replaces`:** the old atom goes back to `active`, `valid_until` is cleared, and the edge gets `retired_at`. **`same`:** the merged new atom goes back to `active`, the evidence added to the old atom is reversed (the pre-merge α/β are stored in the edge metadata), and the edge is retired. Anything else returns 409. |
| `GET /v1/agents/{id}/conflicts?open=true&limit=20` | | Open conflicts ranked by the lower confidence of the two sides, then whether their origin classes differ, then age. |
| `POST /v1/agents/{id}/remember` | new field `resolves: [uuid, uuid]?` | Closes the open conflict between those two atoms. The new atom supersedes neither side. |

**Response additions:**
- **`remember` atoms:** `kind`, `kind_p`, `valid_from`, `date_precision`, and `relations: [{relation, target_atom_id, edge_id?, decision_id}]`, plus `relations_pending: [atom_id]` at the top level. In shadow mode, `relations` holds the shadow decisions, marked `"mode": "shadow"`.
- **`recall` / `recent` atoms:** `kind`, `valid_from`, `date_precision` and `status`.
  - With `include_superseded`, a hidden atom carries `hidden_by: {relation, by_atom_id, edge_id}`.
  - An atom in an open conflict brings its partner once, with `match_type: "conflict_partner"` and `conflict_id`. Partners of partners are not followed.

**MCP** (mnemo-client `v2/phase2`; release 0.7.0rc1 at its own gate):
- `mnemo_supersede(atom_id, reason, by_atom_id=None, text=None)`
- `mnemo_undo_relation(edge_id, reason=None)`
- `mnemo_conflicts(open=True, limit=20)`
- `mnemo_remember` gains `resolves`.
- The instructions string explains conflicts and undo.

## 8. Confidence

- **Prior by `origin_class`** replaces the decomposer's hedging-based confidence, which is dropped from the decomposer schema. Proposed α/β before scaling:

  | origin_class | α | β |
  |---|---|---|
  | `measurement` | 8 | 1 |
  | `document` | 6 | 1.5 |
  | `professional` | 6 | 2 |
  | `user_stated` | 5 | 2 |
  | `unspecified` | 4 | 2 |
  | `agent_inference` | 3 | 2 |

  `unspecified` matches today's most common band, so a 0.5.0 client sees no change.
- **Scaling by grounding:** α′ = 1 + (α − 1) · s, where s = 1 when Jev's grounding probability is at or above its cut-off (0.09 to start), and s = 0.5 below it.
- **Ungrounded quotes.** The deterministic `ungrounded_quote` still lowers one band (Phase 1 behaviour). `quote_inexact` stays record-only.
- **The Jev grounding result is a flag, not a gate:** `verification.grounded_p`, plus the flag `model_ungrounded` when it falls below the cut-off.
- **Evidence** is added only on `same` from an independent source: a different `source_id` and a different store. It reuses `bayesian_merge_damped` (`atom_service.py`).
- **`replaces` and `conflict`** change neither atom's confidence.

## 9. Backfill (G4; each step its own dry run, guarded execute and undo table)

1. **Date precision for haiku_v1 atoms.** Deterministic SQL, `repair_precision_2026_12`.
   - Null `remembered_on` → `unknown`.
   - 1 January 00:00 → `year`.
   - The 1st of another month → `month`.
   - Either of those stays `day` if the text contains an explicit day-and-month date matching that day.
   - Everything else → `day`.
   - `valid_from` is set from `remembered_on`.
   - Counts are reported per agent, with the 42 repaired clio atoms on 1 January shown separately.
2. **Time unresolved.** The 142 prefilter candidates (re-counted) get the `time_unresolved` question, run in the container, and `verification.time_unresolved` is set from the answer. Undo table.
3. **Kind for all active atoms.** About 9,050 requests minus those already classified in shadow or enforce. Jev costs about $0.02 per 1,000, so this is **under $0.25**; the estimate is re-checked against the shadow week's actual `usage.cost`. It stores `kind` / `kind_p`, with an undo table.
4. **Re-decide legacy pairs.** 187 `supersedes`, 2,204 `tension_with`, 3,400 `narrows` and 430 `merge_pairs`, about 6.2k pairs and **about $0.15** (counts as of today).
   - It runs v2 in shadow and writes `relation_decisions` with `decided_by = 'relation_v2_backfill'`.
   - It reports a confusion table of legacy type against v2's final relation, then **STOP**.
   - Applying anything, and retiring legacy edges (`retired_at`; nothing deleted), is Tom's decision.

## 10. Fixtures → mechanism

These are new synthetic cases in `tests/fixtures/v2/`. Live cases must pass 5 of 5. Every case also runs offline with `FakeDecisions` answers, to test the matrix and plumbing.

| case | what makes it pass |
|---|---|
| negation (state) → `replaces` | cell state/state †: same subject, same property, same origin |
| updated number → `replaces` | † plus the date-order guard (newer `valid_from`) |
| planned then done → `replaces` | observation/plan ‡: the change question says yes |
| rescheduled plan → `replaces` | plan/plan † (same plan, moved) |
| series on two days → no hide | observation/observation § fails (different days) |
| reading against baseline → no `replaces` | observation/state ‡: the change question says no (different quantity) |
| "tomorrow" note then dated plan → no `replaces` | `time_unresolved` guard → `review` |
| older over newer → `review` | date-order guard |
| observation against rule → no retire | observation/rule allows only refines or conflict |
| calculation against measurement → `conflict` | origin guard; recall returns both, partner shown once |
| observation correction → `replaces` | §: same day, `day` precision, correction says yes |
| year-precision date → no auto `replaces` | precision guard → `review` |
| duplicate, same source → `same`, no evidence | `same` + independence check fails |
| duplicate, independent source → `same`, evidence | `same` + `bayesian_merge_damped` |
| model outage → stored, pending, 201 | `FakeDecisions` raises; fail open |
| undo → old atom back in recall | `/edges/{id}/undo` + status |
| regression | every Phase 1 fixture |

The fixture runner gains `relations` assertions (expected final relation per pair) and `recall_contains` / `recall_hides`.

## 11. Order of work after G1

1. Migration 011 and the status sync, with tests.
2. Decision client with the ZDR proof.
3. `relations.py`, matrix tests.
4. Kind bake-off → **STOP** with numbers.
5. Decomposer v3 dates.
6. Store-path integration, pending worker, fail open.
7. API and MCP.
8. Fixtures.
9. Synthetic latency numbers.
10. **G2 package:** migration dry run, CI, fixtures, latency, plus the 15 Phase 1 pairs re-decided on synthetic stand-ins. The real pairs are decided only after Tom answers open question 1.

## 12. Open questions for Tom

1. **Production text to OpenRouter.**
   - Phase 1's standing rule was "no production memory text to any provider other than Anthropic". Spec rule 4 implies it now goes to Jev and d1.2-ultra with `zdr: true`. Please confirm explicitly.
   - Also confirm that the OpenRouter key may go in the production `.env`. In Phase 1 it lived only in `~/.config/mnemo` for eval.
2. **Your 15 judged Phase 1 pairs.** Which two did you confirm wrong? Edge ids, or your verdict file's path (kept outside the repo), are enough.
3. **Undated atoms.** Should `valid_from` fall back to the store time at `day` precision, as §4 proposes? The alternative, NULL / `unknown`, means no automatic `replaces` for undated notes.
4. **`same` for `unknown` kinds.** Allow it when both sides are `unknown`? Proposal: yes in enforce only when both kinds are equal **and known**. Otherwise `unknown`/`unknown` duplicates would never merge until backfill 8.3, but the lazy candidate classification in §4 makes that rare.
5. **`decided_by` on existing edges.** Only the lifecycle types get `legacy_lifecycle`, and structural edges stay NULL (§1). Is that acceptable?
6. **Context docs.** `mnemo-v2-plan.md` and `mnemo-v2-status-2026-11-24.md` are not in the repo. I proceeded without them; send them if they matter.
7. **mnemo-client base.** `v2/phase2` is stacked on `v2/phase1` (PR #2, unmerged; 0.6.0rc1 was published from it). Should PR #2 be merged to `main` first?
