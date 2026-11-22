# Mnemo v2 Phase 1 — decision-model bake-off

**Date:** 2026-11-22. **Spec:** `docs/mnemo-v2-phas1-spec.md` item 9. **Script:** `scripts/eval/decision_bakeoff.py`.
No production integration: nothing here runs in the write or read path (spec rule 6).

**Status: round 2.** Per Tom's G2 review, no model is chosen yet. Round 2:
- fixes a harness bug in the generative baseline;
- adds `claude-haiku-5-5` as a second baseline;
- balances the Task A perturbations, with 45 abbreviation cases;
- reports ROC AUC and paired McNemar tests;
- tunes the Task A threshold on held-out data;
- scores Task B against Tom's hand labels.

A **data-policy screen** marks two candidates ineligible for mnemo data. Benchmarking a local open-weight model is out of scope (Tom, 2026-11-22).

## Round 2 findings

1. **Round 1's Haiku 4.5 "negation" result was a harness bug.** The baseline schema asked for `probability_true`, "the probability that the answer is true". Haiku read that as confidence in its own answer, so a confident "not supported" came back as 0.99 and was scored as P(supported).
   - Evidence from the 10 misses inspected: all were plain negations ("did not make a stained glass window", "love and acceptance are NOT everywhere"). The same items scored 0.01 under the reworded question. Haiku's p-values were bimodal at 0.0–0.1 and 0.9–1.0.
   - The schema now asks for `p_supported`, defined explicitly, and keeps the boolean answer for audit. In round 2, none of the two baselines' boolean answers disagree with their probabilities.
   - With the fix, Haiku 4.5 reaches 86.9% on Task A (round 1: 69.7%) and 100% recall on negations (round 1: 36%).
2. **Task A, grounding: no clear winner. Ranking separates the models less than the 0.5 threshold suggests.**
   - ROC AUC sits between 0.954 and 0.966 for every model except `microsoft/microsoft-decision-1.1` (0.913). `liquid/d1.2-ultra` is highest at 0.966.
   - At the 0.5 threshold, `openai/gpt-6-luna-decisions` is most accurate (89.7%). Under paired McNemar it is not significantly better than `google/gemini-decision-1`, `liquid/d1.2-ultra` or Haiku 4.5.
   - It is significantly better (Bonferroni p < 0.0083) than `typesafe/jev-1.14`, `microsoft/microsoft-decision-1.1` and Haiku 5.5. Those three lose mainly on *supported* items (recall 73–77%), which means a stricter implicit threshold. The AUCs show their ranking is as good.
   - Any Phase 2 use should set the threshold per model on held-out data, not use 0.5.
   - Every model catches 100% of negations and about 87–100% of number and abbreviation changes. Subject swaps are the hardest (82–89%).
3. **Task B, relation, scored against Tom's hand labels: no model is significantly better than any other.**
   - Tom labelled 47 pairs: the 27 where at least two models disagreed with the generated label, plus 20 random. All 20 random pairs matched the generated label. He changed 12 of the 27 disagreements, 8 of them away from `contradicts` (to `update` ×4, `unrelated` ×2, `narrows`, `same`). So part of round 2's weak contradiction recall was label error.
   - With his labels, accuracy is 91.0–94.0%: `microsoft/microsoft-decision-1.1` 94.0%; `typesafe/jev-1.14` and `liquid/d1.2-ultra` 93.0%. Every McNemar p ≥ 0.14.
   - Macro AUC is 0.977–0.994.
   - Jev has the best contradiction recall (90.9%) and a low Brier score (0.104; Microsoft 0.098).
   - On the 47 hard pairs alone: Microsoft 36, Jev 34, Liquid 33, Haiku 4.5 33, Gemini 31, Haiku 5.5 31, Luna 30.
4. **Option order barely matters** (0–1.5% of labels flip, except `openai/gpt-6-luna-decisions` at 6.5% on Task B). **Rewording matters more:** 1.3–6.7% flips on Task A and 4.0–10.5% on Task B.
5. **Cost and latency.** Decision models answer in 0.17–0.28 s at p50 and cost $0.007–0.028 per 1,000 decisions. The Haiku baselines take 0.56–1.15 s and cost $0.12 (5.5) to $1.50 (4.5) per 1,000.

What this leaves for the model choice:
- **Eligible for mnemo data** (ZDR endpoints, routing enforceable with `provider: {"zdr": true}`): `typesafe/jev-1.14`, `liquid/d1.2-ultra`, `microsoft/microsoft-decision-1.1`. See the data-policy table.
- `google/gemini-decision-1` and `openai/gpt-6-luna-decisions` are **ineligible**: no ZDR endpoint.
- **Grounding**, with a held-out tuned threshold: Jev reaches 89.5%, with a stable threshold of about 0.09; Liquid 88.4%, with an unstable threshold.
- **Relation:** the three eligible models are statistically tied, at 93–94%.
- Haiku 5.5, through Anthropic, remains the generative option at about 5–20× their cost.

## Data policy (mnemo data eligibility)

Checked 2026-11-22. Sources:
- OpenRouter's model endpoints API (`/api/v1/models/{id}/endpoints`, the routed providers).
- The public ZDR endpoint list (`/api/v1/endpoints/zdr`, 936 endpoints).
- The ZDR and data-collection docs (`/docs/features/zdr`, `/docs/guides/privacy/data-collection`).
- A live test of `provider` preferences on `POST /api/alpha/decisions` with a one-line synthetic state.

OpenRouter itself does not store prompts or responses unless the account opts in to logging. It keeps metadata only (`/docs/guides/privacy/data-collection`). Tiers from the ZDR doc:
- **ZDR:** no routine storage after processing, no training.
- **Safety Retention:** stored only for abuse detection, no training.
- **Retention and Training:** stored and used for training only as the provider's own policy says.

| candidate | routed provider (only endpoint) | ZDR list | retention / training of inputs | `provider: {"zdr": true}` on Decisions | eligible for mnemo data |
|---|---|---|---|---|---|
| `typesafe/jev-1.14` | TypeSafe (`jev-1.14-20261014`) | yes, `safety_retention: false` | ZDR tier: no routine retention, no training | accepted, routed to TypeSafe | **yes** |
| `liquid/d1.2-ultra` | Liquid (`d1.2-ultra-20261116`) | yes, `safety_retention: false` | ZDR tier: no routine retention, no training | accepted, routed to Liquid | **yes** |
| `microsoft/microsoft-decision-1.1` | Azure (`microsoft-decision-1.1-20261117`) | yes, `safety_retention: false` | ZDR tier: no routine retention, no training | accepted, routed to Azure | **yes** |
| `google/gemini-decision-1` | Google AI Studio (`gemini-decision-1-20261111`) | **no** (the ZDR doc's Google group "removes AI Studio endpoints") | per Google's AI Studio terms; not covered by OpenRouter ZDR | **rejected**: 404 "No endpoints found matching your data policy (Zero data retention)" | **no — ineligible** |
| `openai/gpt-6-luna-decisions` | OpenAI first-party (`gpt-6-luna-decisions-20261006`) | **no** (the ZDR doc's OpenAI group "removes first-party OpenAI endpoints") | per OpenAI's API terms; not covered by OpenRouter ZDR | **rejected**: 404 "No endpoints found for openai/gpt-6-luna-decisions." | **no — ineligible** |

Enforcement:
- `provider: {"zdr": true}` **is enforced** on the Decisions endpoint: the two non-ZDR models are refused and the three ZDR models still answer. Any Phase 2 integration must send it on every request. Setting ZDR enforcement at the account level (`https://openrouter.ai/settings/privacy`) adds a second layer, because it is OR-combined with the per-request flag.
- `provider: {"data_collection": "deny"}` **did not** exclude Gemini or Luna in the same test. The endpoint also silently accepts unknown `provider` fields: a bogus field returned 200. Do not rely on `data_collection` here.
- The bake-off itself sent only synthetic text and LoCoMo text from `mnemo_eval`, without `zdr`. That was within rule 4, since no production memory text was involved.

## Task A threshold tuning (Jev and Liquid)

The threshold on P(supported) is tuned on one half of the 390 Task A items and scored on the other. The split is stratified by label and perturbation, and both folds are reported (`decision_bakeoff.py threshold`).

Task A threshold tuning: halves of 193 / 197 items, stratified by label and perturbation (seed 5).

| model | fold (tune → test) | tuned threshold | test accuracy, tuned | test accuracy, 0.5 | test recall supported / unsupported, tuned |
|---|---|---|---|---|---|
| typesafe/jev-1.14 | 1: half 1 → half 2 | 0.0900 | 90.4% (n=197) | 85.8% | 98.1% / 81.5% |
| typesafe/jev-1.14 | 2: half 2 → half 1 | 0.0950 | 88.6% (n=193) | 83.9% | 95.2% / 80.7% |
| typesafe/jev-1.14 | mean of both folds | | 89.5% | | |
| liquid/d1.2-ultra | 1: half 1 → half 2 | 0.0970 | 91.4% (n=197) | 89.8% | 93.3% / 89.1% |
| liquid/d1.2-ultra | 2: half 2 → half 1 | 0.1556 | 85.5% (n=193) | 86.0% | 84.8% / 86.4% |
| liquid/d1.2-ultra | mean of both folds | | 88.4% | | |
- **Jev's** best threshold is low and stable across the two folds (0.090 / 0.095). It lifts held-out accuracy by about 4–5 points over 0.5, to 89.5% on average. At 0.5 it rejected too many supported atoms.
- **Liquid's** best threshold moves between folds (0.097 / 0.156). Its held-out accuracy averages 88.4%, and in one fold the tuned threshold did slightly worse than 0.5. Its default threshold is already close to right.
- Each half is about 195 items, so a 95% interval is roughly ±4.5 points. These two tuned results are not distinguishable from each other or from the best untuned model (89.7%).

## Setup

- **API.** OpenRouter Decisions (alpha), `POST https://openrouter.ai/api/alpha/decisions`, docs read 2026-11-22 at `https://openrouter.ai/docs/api-reference/decisions`.
  - `noul` returns P(true).
  - `choice` returns per-option `probabilities`.
  - `usage.cost` is the charged cost.
  - Latency is client wall time per request, including network, at concurrency 8.
  - The model list comes from `GET /api/v1/models?output_modalities=decisions` (45 models on 2026-11-22).
- **Candidates.**
  - `typesafe/jev-1.14`, from the spec.
  - Four other recent, non-free, general decision models: `google/gemini-decision-1`, `openai/gpt-6-luna-decisions`, `liquid/d1.2-ultra`, `microsoft/microsoft-decision-1.1`.
  - Generative baselines answering the same questions as JSON through a forced tool call:
    - `claude-haiku-4-5-20251001`, the Phase 1 decomposer ($1 / $5 per million tokens);
    - `claude-haiku-5-5`, the decomposer from G2 ($0.10 / $0.50).
- **Data (rule 4).** Only synthetic text and LoCoMo text from `mnemo_eval`. The script refuses any other database. No production memory text went to any provider. LoCoMo-derived items and raw results stay outside the repo (`~/.cache/mnemo-bakeoff`); round 1 results are kept as `results_r1.jsonl`.
  - **Task A, grounding (390 items).**
    - Positives: 210 decomposer v2 (Haiku 4.5) atoms whose `source_quote` passes the `ungrounded_quote` check. They come from 64 synthetic notes (`synthetic_notes.json`, plus the abbreviation-rich `synthetic_abbrev_notes.json`) and 24 LoCoMo passages (4 consecutive `mnemo_eval` atoms each).
    - Negatives: **45 per perturbation type**, each from a distinct positive:
      - number changed (deterministic edit);
      - abbreviation replaced by a wrong expansion or a look-alike code (VTI→VTV, DKIM→DKML; correct expansions rejected);
      - subject swapped;
      - negation (edits by Haiku 4.5).
    - The judge sees a `CONTEXT:` line (speaker, addressee, date) because v2 atoms take names and dates from the request.
  - **Task B, relation (200 pairs, 40 per class).** Synthetic pairs generated by Haiku 4.5 from the class definitions in `RELATION_SPEC`, across 10 domains (`relation_pairs.jsonl`).
- **Variants.** `base`; `order` (options permuted); `wording` (question and labels reworded, mapped back for scoring).
- **Metrics.**
  - Accuracy and per-class recall (Task A at a 0.5 threshold; Task B arg-max).
  - ROC AUC: Task A on P(supported); Task B macro one-vs-rest.
  - Brier score; ECE over 10 bins with a reliability table.
  - Flip rate per variant.
  - Exact McNemar test against the most accurate model, paired on base-variant items, with unanswered counting as wrong.
  - p50/p95 latency; cost per 1,000.

## Round 2 results

_Generated 2026-11-22 17:31 from 12390 results._

_Task B uses Tom's hand labels for the 47 checked pairs (12 differ from the generated label); the other pairs keep the generated label._

### Task A — grounding (noul: is the atom fully supported by the quote?)

| model | n | answered | accuracy | ROC AUC | recall supported | recall unsupported | Brier | ECE | flip: order | flip: wording | mean abs Δp (order / wording) | p50 s | p95 s | $ / 1k |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-haiku-4-5-20251001 | 390 | 100.0% | 86.9% | 0.954 | 79.5% | 95.6% | 0.109 | 0.091 | 0.5% | 2.3% | 0.007 / 0.028 | 0.77 | 0.82 | 1.1854 |
| claude-haiku-5-5 | 390 | 100.0% | 83.6% | 0.959 | 72.9% | 96.1% | 0.137 | 0.113 | 0.5% | 4.1% | 0.007 / 0.043 | 0.56 | 0.76 | 0.1155 |
| google/gemini-decision-1 | 390 | 100.0% | 88.5% | 0.962 | 85.2% | 92.2% | 0.086 | 0.048 | 0.0% | 5.6% | 0.000 / 0.050 | 0.26 | 0.37 | 0.0236 |
| liquid/d1.2-ultra | 390 | 100.0% | 87.9% | 0.966 | 82.9% | 93.9% | 0.094 | 0.072 | 0.0% | 1.8% | 0.002 / 0.024 | 0.22 | 0.59 | 0.0067 |
| microsoft/microsoft-decision-1.1 | 390 | 100.0% | 84.6% | 0.913 | 74.3% | 96.7% | 0.141 | 0.126 | 0.0% | 1.5% | 0.000 / 0.039 | 0.28 | 0.32 | 0.0066 |
| openai/gpt-6-luna-decisions | 390 | 100.0% | 89.7% | 0.957 | 88.1% | 91.7% | 0.086 | 0.066 | 0.0% | 6.7% | 0.000 / 0.062 | 0.21 | 0.42 | 0.0282 |
| typesafe/jev-1.14 | 390 | 100.0% | 84.9% | 0.959 | 76.7% | 94.4% | 0.107 | 0.072 | 0.0% | 1.3% | 0.010 / 0.031 | 0.18 | 0.21 | 0.0176 |

390 items × 3 variants. Accuracy, recall, Brier and ECE are on the base variant; unanswered items (refusal, missing answer, error) count against recall and the answered rate, not accuracy. Flip = share of answered items whose predicted label changes under the variant.

Paired McNemar tests against the most accurate model, **openai/gpt-6-luna-decisions** (89.7%); b = best right / other wrong, c = the reverse:

| model | accuracy | b | c | exact p |
|---|---|---|---|---|
| google/gemini-decision-1 | 88.5% | 23 | 18 | 0.533 |
| liquid/d1.2-ultra | 87.9% | 22 | 15 | 0.324 |
| claude-haiku-4-5-20251001 | 86.9% | 24 | 13 | 0.0989 |
| typesafe/jev-1.14 | 84.9% | 29 | 10 | 0.00338 |
| microsoft/microsoft-decision-1.1 | 84.6% | 32 | 12 | 0.00366 |
| claude-haiku-5-5 | 83.6% | 36 | 12 | 0.000717 |

6 comparisons; at a Bonferroni-adjusted 0.05 the threshold is p < 0.00833.

Recall on unsupported items, by perturbation:

| model | number | abbreviation | subject | negation |
|---|---|---|---|---|
| claude-haiku-4-5-20251001 | 97.8% (n=45) | 95.6% (n=45) | 88.9% (n=45) | 100.0% (n=45) |
| claude-haiku-5-5 | 97.8% (n=45) | 97.8% (n=45) | 88.9% (n=45) | 100.0% (n=45) |
| google/gemini-decision-1 | 86.7% (n=45) | 95.6% (n=45) | 86.7% (n=45) | 100.0% (n=45) |
| liquid/d1.2-ultra | 91.1% (n=45) | 95.6% (n=45) | 88.9% (n=45) | 100.0% (n=45) |
| microsoft/microsoft-decision-1.1 | 100.0% (n=45) | 97.8% (n=45) | 88.9% (n=45) | 100.0% (n=45) |
| openai/gpt-6-luna-decisions | 93.3% (n=45) | 91.1% (n=45) | 82.2% (n=45) | 100.0% (n=45) |
| typesafe/jev-1.14 | 95.6% (n=45) | 95.6% (n=45) | 86.7% (n=45) | 100.0% (n=45) |

<details><summary>Reliability tables (confidence of the predicted label vs. accuracy)</summary>

**claude-haiku-4-5-20251001**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.8-0.9 | 36 | 0.850 | 0.361 |
| 0.9-1.0 | 354 | 0.971 | 0.921 |

**claude-haiku-5-5**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.8-0.9 | 29 | 0.859 | 0.345 |
| 0.9-1.0 | 361 | 0.957 | 0.875 |

**google/gemini-decision-1**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.5-0.6 | 17 | 0.541 | 0.706 |
| 0.6-0.7 | 23 | 0.660 | 0.652 |
| 0.7-0.8 | 34 | 0.758 | 0.618 |
| 0.8-0.9 | 52 | 0.858 | 0.731 |
| 0.9-1.0 | 264 | 0.964 | 0.981 |

**liquid/d1.2-ultra**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.5-0.6 | 8 | 0.538 | 0.500 |
| 0.6-0.7 | 6 | 0.648 | 0.667 |
| 0.7-0.8 | 16 | 0.754 | 0.438 |
| 0.8-0.9 | 30 | 0.862 | 0.567 |
| 0.9-1.0 | 330 | 0.984 | 0.942 |

**microsoft/microsoft-decision-1.1**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.5-0.6 | 7 | 0.542 | 0.429 |
| 0.6-0.7 | 8 | 0.658 | 0.750 |
| 0.7-0.8 | 10 | 0.752 | 0.700 |
| 0.8-0.9 | 12 | 0.871 | 0.667 |
| 0.9-1.0 | 353 | 0.993 | 0.867 |

**openai/gpt-6-luna-decisions**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.5-0.6 | 10 | 0.557 | 0.600 |
| 0.6-0.7 | 9 | 0.653 | 0.667 |
| 0.7-0.8 | 11 | 0.743 | 0.727 |
| 0.8-0.9 | 18 | 0.851 | 0.667 |
| 0.9-1.0 | 342 | 0.993 | 0.930 |

**typesafe/jev-1.14**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.5-0.6 | 9 | 0.530 | 0.667 |
| 0.6-0.7 | 17 | 0.653 | 0.412 |
| 0.7-0.8 | 30 | 0.746 | 0.500 |
| 0.8-0.9 | 43 | 0.849 | 0.558 |
| 0.9-1.0 | 291 | 0.968 | 0.959 |

</details>

### Task B — relation (choice: same / update / contradicts / narrows / unrelated)

| model | n | answered | accuracy | macro ROC AUC | recall same | recall update | recall contradicts | recall narrows | recall unrelated | Brier | ECE | flip: order | flip: wording | p50 s | p95 s | $ / 1k |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-haiku-4-5-20251001 | 200 | 100.0% | 92.5% | 0.977 | 92.1% | 90.9% | 100.0% | 93.0% | 88.1% | 0.147 | 0.091 | 4.0% | 8.0% | 1.15 | 1.22 | 1.4998 |
| claude-haiku-5-5 | 200 | 100.0% | 91.0% | 0.978 | 97.4% | 93.2% | 75.8% | 93.0% | 92.9% | 0.176 | 0.149 | 3.5% | 4.0% | 0.68 | 0.83 | 0.1496 |
| google/gemini-decision-1 | 200 | 100.0% | 91.0% | 0.984 | 92.1% | 97.7% | 78.8% | 90.7% | 92.9% | 0.146 | 0.057 | 0.0% | 5.5% | 0.27 | 0.46 | 0.0261 |
| liquid/d1.2-ultra | 200 | 100.0% | 93.0% | 0.990 | 94.7% | 97.7% | 81.8% | 95.3% | 92.9% | 0.124 | 0.097 | 1.5% | 5.5% | 0.20 | 0.25 | 0.0083 |
| microsoft/microsoft-decision-1.1 | 200 | 100.0% | 94.0% | 0.994 | 92.1% | 95.5% | 87.9% | 95.3% | 97.6% | 0.098 | 0.036 | 0.0% | 5.5% | 0.28 | 0.33 | 0.0084 |
| openai/gpt-6-luna-decisions | 200 | 100.0% | 91.0% | 0.984 | 97.4% | 95.5% | 72.7% | 93.0% | 92.9% | 0.149 | 0.059 | 6.5% | 10.5% | 0.24 | 0.43 | 0.0279 |
| typesafe/jev-1.14 | 200 | 100.0% | 93.0% | 0.989 | 89.5% | 97.7% | 90.9% | 95.3% | 90.5% | 0.104 | 0.041 | 1.5% | 7.5% | 0.17 | 0.20 | 0.0212 |

200 items × 3 variants. Accuracy, recall, Brier and ECE are on the base variant; unanswered items (refusal, missing answer, error) count against recall and the answered rate, not accuracy. Flip = share of answered items whose predicted label changes under the variant.

Paired McNemar tests against the most accurate model, **microsoft/microsoft-decision-1.1** (94.0%); b = best right / other wrong, c = the reverse:

| model | accuracy | b | c | exact p |
|---|---|---|---|---|
| liquid/d1.2-ultra | 93.0% | 7 | 5 | 0.774 |
| typesafe/jev-1.14 | 93.0% | 5 | 3 | 0.727 |
| claude-haiku-4-5-20251001 | 92.5% | 11 | 8 | 0.648 |
| claude-haiku-5-5 | 91.0% | 9 | 3 | 0.146 |
| google/gemini-decision-1 | 91.0% | 9 | 3 | 0.146 |
| openai/gpt-6-luna-decisions | 91.0% | 10 | 4 | 0.18 |

6 comparisons; at a Bonferroni-adjusted 0.05 the threshold is p < 0.00833.

<details><summary>Reliability tables (confidence of the predicted label vs. accuracy)</summary>

**claude-haiku-4-5-20251001**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.6-0.7 | 10 | 0.630 | 0.700 |
| 0.7-0.8 | 46 | 0.739 | 0.826 |
| 0.8-0.9 | 87 | 0.830 | 0.954 |
| 0.9-1.0 | 57 | 0.952 | 1.000 |

**claude-haiku-5-5**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.3-0.4 | 2 | 0.365 | 1.000 |
| 0.4-0.5 | 2 | 0.425 | 0.500 |
| 0.5-0.6 | 16 | 0.529 | 0.500 |
| 0.6-0.7 | 23 | 0.605 | 0.826 |
| 0.7-0.8 | 30 | 0.726 | 0.833 |
| 0.8-0.9 | 103 | 0.829 | 1.000 |
| 0.9-1.0 | 24 | 0.914 | 1.000 |

**google/gemini-decision-1**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.3-0.4 | 5 | 0.366 | 0.200 |
| 0.4-0.5 | 4 | 0.453 | 0.500 |
| 0.5-0.6 | 8 | 0.562 | 0.750 |
| 0.6-0.7 | 14 | 0.654 | 0.857 |
| 0.7-0.8 | 24 | 0.746 | 0.792 |
| 0.8-0.9 | 26 | 0.861 | 0.923 |
| 0.9-1.0 | 119 | 0.963 | 0.992 |

**liquid/d1.2-ultra**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.3-0.4 | 2 | 0.348 | 0.000 |
| 0.4-0.5 | 6 | 0.459 | 0.667 |
| 0.5-0.6 | 13 | 0.545 | 0.615 |
| 0.6-0.7 | 17 | 0.645 | 0.824 |
| 0.7-0.8 | 18 | 0.748 | 0.944 |
| 0.8-0.9 | 41 | 0.861 | 0.976 |
| 0.9-1.0 | 103 | 0.948 | 1.000 |

**microsoft/microsoft-decision-1.1**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.2-0.3 | 1 | 0.278 | 0.000 |
| 0.4-0.5 | 2 | 0.473 | 0.500 |
| 0.5-0.6 | 9 | 0.546 | 0.667 |
| 0.6-0.7 | 16 | 0.661 | 0.688 |
| 0.7-0.8 | 12 | 0.748 | 0.833 |
| 0.8-0.9 | 18 | 0.850 | 1.000 |
| 0.9-1.0 | 142 | 0.989 | 1.000 |

**openai/gpt-6-luna-decisions**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.3-0.4 | 3 | 0.367 | 0.000 |
| 0.4-0.5 | 5 | 0.436 | 0.600 |
| 0.5-0.6 | 11 | 0.545 | 0.818 |
| 0.6-0.7 | 19 | 0.642 | 0.737 |
| 0.7-0.8 | 16 | 0.762 | 0.875 |
| 0.8-0.9 | 17 | 0.846 | 0.824 |
| 0.9-1.0 | 129 | 0.970 | 0.992 |

**typesafe/jev-1.14**

| bin | n | mean confidence | accuracy |
|---|---|---|---|
| 0.4-0.5 | 3 | 0.440 | 0.667 |
| 0.5-0.6 | 10 | 0.553 | 0.500 |
| 0.6-0.7 | 9 | 0.666 | 0.444 |
| 0.7-0.8 | 10 | 0.738 | 0.800 |
| 0.8-0.9 | 19 | 0.861 | 1.000 |
| 0.9-1.0 | 149 | 0.982 | 0.993 |

</details>

## Round 1 (superseded)

Round 1 (290 Task A items, unbalanced, 6 abbreviation cases; Haiku 4.5 as the only baseline) put the decision models at 85–91% on Task A and 90.5–94.5% on Task B. Its Haiku 4.5 Task A result (69.7%) was the harness bug described above. Raw results are in `~/.cache/mnemo-bakeoff/results_r1.jsonl`.

## Fixture pass rates: generative decomposers (Phase 1 fixtures)

These are `tests/eval/test_v2_fixtures.py` runs, with each live case run 5 times. Both decomposers use prompt v2, the production dedup threshold (0.97) and lifecycle detection on.

Counts exclude the 10 runs of the two cases still marked xfail; the provider-failure case runs once.

| decomposer | passed | failed | expected-fail (phase 2) | notes |
|---|---|---|---|---|
| `claude-haiku-4-5-20251001` (current production) | 70 / 71 | `attribution_no_label` 1 of 5 | `validity_window` 5 / 5 xfail | `planned_then_done` passed 5 / 5 (non-strict xfail); `updated_number@0.90` passed 5 / 5 (xfail since removed). Across all probes `attribution_no_label` held about 92% (35 / 38). |
| `claude-haiku-5-5` (current Haiku) | 71 / 71 | none | `validity_window` 5 / 5 xfail | `planned_then_done` passed 5 / 5; every attribution, abbreviation, reported-speech, temporary-state, correction and long-note case 5 / 5. |

- With prompt v2 on synthetic notes, Haiku 5.5 also lowers mean `remember` latency from 2.41 s to 1.39 s (model call 2.26 s → 1.20 s).
- At $0.10 / $0.50 per million tokens it costs a tenth as much as Haiku 4.5 ($1 / $5).
- Proposed at G2: switch `MNEMO_DECOMPOSER_MODEL` to `claude-haiku-5-5`. The lifecycle detector keeps its own model, unchanged.

## Limitations

- **Labels.**
  - Task B labels: Tom hand-checked 47 pairs (all 27 model-disagreement pairs, plus 20 random). The other 153 keep the generated label. On the random sample the generator was 20 for 20 correct.
  - On Task A, a "supported" item means the generating decomposer produced the atom and its quote is verbatim in the source. Decomposer errors therefore become "supported" positives. Example: one atom attributes Tom's "my CGM sensor" to Clio. Recall on supported items is a floor, and some "misses" are correct rejections.
  - A follow-up could generate positives with the Haiku 5.5 decomposer, which attributes better (see the fixtures), or hand-check the positives.
- **Generative baselines' probabilities are stated, not sampled.** Their calibration is not like-for-like with the decision models; their accuracy and AUC are.
- **Model versions.** Alpha API, date-stamped model builds. Re-run before Phase 2 relies on these numbers: `run` resumes, `report` regenerates.
