# Phase 2 kind bake-off

**Date:** 2026-11-24. **Spec:** `docs/phase2-spec.md` item 4. **Design:** `docs/phase2-design.md` §2.1, §4.
**Script:** `scripts/eval/kind_bakeoff.py`. **Data:** `scripts/eval/data/kind_atoms.jsonl`, 160 synthetic atoms.
**Status:**
- Tom chose `typesafe/jev-1.14`, fallback `liquid/d1.2-ultra`, kind threshold **0.80 provisionally** (2026-11-24).
- The threshold is fixed only after this re-score against his hand labels (below) has been reported.

## Re-score against Tom's hand labels (2026-11-24)

Tom labelled all 27 rows of `kind_label_check.csv`: the 7 atoms on which two or more models disagreed with the generated label, plus 20 random ones. His labels replace the generated ones for those 27; the other 133 keep the generated label.

- **He changed 8 labels.** 4 were among the 7 disagreements and **4 among the 20 random rows**. That suggests the generated labels on the unchecked 133 are wrong about 20% of the time (4 of 20; the interval is wide). Every accuracy figure below carries that uncertainty.
- **Alignment check.** His labels are not shifted by a row. Agreement with the generated label and the three models is 0.69 on the same row, and 0.23–0.39 one or two rows off.

| model | answered | accuracy | recall state | recall plan | recall observation | recall rule | Brier | ECE | flip: order | flip: wording | coverage ≥ thr | accuracy ≥ thr | p50 s | p95 s | $ / 1k |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| typesafe/jev-1.14 | 100.0% | 94.4% | 94.4% | 95.5% | 95.0% | 92.5% | 0.102 | 0.046 | 1.9% | 5.0% | 88.8% | 97.2% | 0.32 | 0.43 | 0.0225 |
| liquid/d1.2-ultra | 100.0% | 93.1% | 88.9% | 95.5% | 95.0% | 92.5% | 0.124 | 0.066 | 1.9% | 2.5% | 83.1% | 97.0% | 0.43 | 1.69 | 0.0098 |
| claude-haiku-5-5 | 100.0% | 94.4% | 91.7% | 95.5% | 95.0% | 95.0% | 0.138 | 0.151 | 2.5% | 3.8% | 69.4% | 96.4% | 0.67 | 0.85 | 0.1416 |

Coverage is measured at the provisional threshold of 0.80.

**On the 27 checked rows alone:** Jev 19/27, Liquid 19/27, Haiku 18/27.

**Threshold:** coverage, accuracy and wrong kinds kept, base variant.

| threshold | typesafe/jev-1.14: kept / accuracy / wrong | liquid/d1.2-ultra: kept / accuracy / wrong | claude-haiku-5-5: kept / accuracy / wrong |
|---|---|---|---|
| 0.5 | 98.8% / 95.6% / 7 | 98.1% / 93.6% / 10 | 100.0% / 94.4% / 9 |
| 0.6 | 95.0% / 95.4% / 7 | 93.8% / 95.3% / 7 | 96.2% / 96.1% / 6 |
| 0.7 | 92.5% / 95.9% / 6 | 88.1% / 96.5% / 5 | 84.4% / 97.0% / 4 |
| 0.8 | 88.8% / 97.2% / 4 | 83.1% / 97.0% / 4 | 69.4% / 96.4% / 4 |
| 0.9 | 84.4% / 97.0% / 4 | 50.6% / 97.5% / 2 | 19.4% / 93.5% / 2 |

- **Jev's wrong kinds at 0.80 are confident ones.** It keeps 4 wrong kinds at 0.8 and still 4 at 0.9 (probabilities 0.97–1.0), so raising the threshold does not remove them. All 4 are rows Tom relabelled where Jev agreed with the generator:
  - `K0011` observation → Tom: plan;
  - `K0026` observation → Tom: state;
  - `K0137` state → Tom: observation;
  - `K0159` state → Tom: rule.
- **What this means.** At 0.80 the open question is not calibration. It is which reading of a few boundary atoms is right: a dated past action, an ongoing fee "until renewal", a cost of running something.
- **The 8 changes by direction** (generated → Tom): state → plan 3, state → observation 1, state → rule 1, observation → plan 1, observation → state 1, rule → observation 1.

## Summary

- **Accuracy.** All three models label about 95% of the atoms the way the generator did: Jev 95.6%, d1.2-ultra 95.0%, Haiku 5.5 95.6%. The paired differences are tiny (McNemar p = 1.0 for both comparisons with Jev).
- **Where the errors are.** Every miss is a state or a rule. All 80 plans and observations are right for all three models. The weak buckets:
  - `state:value` (80% for all three);
  - `state:baseline` (Liquid 60%);
  - `state:status`;
  - `rule:fact`.

  Most misses are a state labelled as a plan or a rule.
- **Calibration** favours Jev: Brier 0.069 and ECE 0.043, against 0.089 / 0.085 for Liquid and 0.107 / 0.163 for Haiku.
  - 135 of Jev's 160 answers are at 0.9 or above, and 99.3% of those are right.
  - Haiku's probabilities are compressed into 0.6–0.9, so a threshold cuts its coverage hard (69% at 0.8).
- **Wording sensitivity** is under the design's 10% bar for all three. Rewording flips Jev 5.0%, Liquid 2.5% and Haiku 3.8%; reordering flips 1.9–2.5%.
- **Latency was higher than in Phase 1.**
  - Jev: p50 0.32 s, p95 0.43 s at concurrency 8. Phase 1 measured 0.17–0.18 s at p50.
  - Liquid: p50 0.43 s, p95 1.69 s.
  - The design's store-path estimate (§5) assumed 0.18 s per request. At today's speed the p50 store would take about 1.4 s rather than 0.8 s, and more large stores would run out of the 3 s budget. This needs re-measuring on the real store path before G2.
- **Cost per 1,000 decisions:** Jev $0.0225, Liquid $0.0098, Haiku $0.1416.

**Caveats:**
- The labels were written by Haiku 5.5, which is also one of the baselines, so its agreement with them is flattered.
- Synthetic atoms are cleaner than production ones.
- 7 items had two or more models disagreeing with the generated label. `scripts/eval/data/kind_label_check.csv` holds those 7 plus 20 random items (27 rows, labels hidden) for Tom to hand-label, as in Phase 1. Scores should be re-run against his labels before a threshold is fixed.

## Kind threshold: coverage against accuracy (base variant)

Atoms below the threshold are stored as `unknown`.

| threshold | jev-1.14: kept / accuracy / wrong kept | d1.2-ultra: kept / accuracy / wrong kept | claude-haiku-5-5: kept / accuracy / wrong kept |
|---|---|---|---|
| 0.5 | 98.8% / 96.8% / 5 | 98.1% / 95.5% / 7 | 100.0% / 95.6% / 7 |
| 0.6 | 95.0% / 96.7% / 5 | 93.8% / 97.3% / 4 | 96.2% / 97.4% / 4 |
| 0.7 | 92.5% / 98.0% / 3 | 88.1% / 98.6% / 2 | 84.4% / 99.3% / 1 |
| 0.8 | 88.8% / 99.3% / 1 | 83.1% / 98.5% / 2 | 69.4% / 100.0% / 0 |
| 0.9 | 84.4% / 99.3% / 1 | 50.6% / 100.0% / 0 | 19.4% / 100.0% / 0 |

With Jev, the design's starting threshold of 0.60 leaves 5% of atoms `unknown` and keeps 5 wrong kinds. At 0.80 it leaves 11% `unknown` and keeps 1 wrong kind.

A wrong kind can only cause harm through the matrix. For example, a state mislabelled as a rule can still be replaced by a rule, but never by an observation. Since `unknown` atoms are only ever linked, the cost of a higher threshold is fewer automatic decisions, not wrong ones.

## Method

- **Atoms.** 160 single-sentence synthetic atoms, generated by `claude-haiku-5-5` through Anthropic, with invented names, spread across ten domains:
  - 4 kinds × 4 boundary buckets × 10 atoms;
  - the buckets target the hard boundaries: baseline vs reading, booked vs attended, habit vs one-off, "currently uses" vs "always".

  Each atom has a note date, and an event date and precision where the generator gave one.
- **Question.** The production `kind` question, verbatim from `mnemo/server/decisions/questions.py` (`QUESTIONS_VERSION = "q1"`), posed on the production per-atom state template (CONTEXT, QUOTE, ATOM, EVENT DATE). The quote is the atom itself.
- **Variants.** `base`; `order` (options reversed); `wording` (reworded instructions and option names, mapped back).
- **Models.**
  - `typesafe/jev-1.14` and `liquid/d1.2-ultra` through `mnemo.server.decisions.OpenRouterDecisions`. Every request carried `provider: {"zdr": true}`, and all 960 succeeded.
  - `claude-haiku-5-5` as a generative baseline, with forced-tool JSON giving the choice and a probability for every option.
- **Measurement.** Latency is client wall time per request at concurrency 8, including network. Cost is OpenRouter's `usage.cost`, or Anthropic tokens × list price.
- **Privacy.** Results (`~/.cache/mnemo-bakeoff/kind_results.jsonl`) hold ids, labels and model outputs only. No production data was read or sent.

## Full results

160 synthetic atoms: observation 40, plan 40, rule 40, state 40. Variants: base, order, wording. Kind threshold for coverage: 0.6.

| model | answered | accuracy | recall state | recall plan | recall observation | recall rule | Brier | ECE | flip: order | flip: wording | coverage ≥ thr | accuracy ≥ thr | p50 s | p95 s | $ / 1k |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| typesafe/jev-1.14 | 100.0% | 95.6% | 90.0% | 100.0% | 100.0% | 92.5% | 0.069 | 0.043 | 1.9% | 5.0% | 95.0% | 96.7% | 0.32 | 0.43 | 0.0225 |
| liquid/d1.2-ultra | 100.0% | 95.0% | 85.0% | 100.0% | 100.0% | 95.0% | 0.089 | 0.085 | 1.9% | 2.5% | 93.8% | 97.3% | 0.43 | 1.69 | 0.0098 |
| claude-haiku-5-5 | 100.0% | 95.6% | 87.5% | 100.0% | 100.0% | 95.0% | 0.107 | 0.163 | 2.5% | 3.8% | 96.2% | 97.4% | 0.67 | 0.85 | 0.1416 |

Answered = base-variant items with a usable answer. Accuracy, recall, Brier and ECE are on the base variant. Flip = share of items whose label changes under the variant. Coverage = share of answered items whose top probability is at least 0.6 (the rest would be stored as `unknown`).

## Paired comparison against the most accurate model, `typesafe/jev-1.14`

| model | b (best right, other wrong) | c (reverse) | exact p |
|---|---|---|---|
| liquid/d1.2-ultra | 4 | 3 | 1.000 |
| claude-haiku-5-5 | 2 | 2 | 1.000 |

## Accuracy by boundary bucket (base variant)

| bucket | n | typesafe/jev-1.14 | liquid/d1.2-ultra | claude-haiku-5-5 |
|---|---|---|---|---|
| observation:action | 10 | 100.0% | 100.0% | 100.0% |
| observation:attended | 10 | 100.0% | 100.0% | 100.0% |
| observation:event | 10 | 100.0% | 100.0% | 100.0% |
| observation:reading | 10 | 100.0% | 100.0% | 100.0% |
| plan:booking | 10 | 100.0% | 100.0% | 100.0% |
| plan:conditional | 10 | 100.0% | 100.0% | 100.0% |
| plan:expectation | 10 | 100.0% | 100.0% | 100.0% |
| plan:intention | 10 | 100.0% | 100.0% | 100.0% |
| rule:fact | 10 | 80.0% | 90.0% | 90.0% |
| rule:habit | 10 | 90.0% | 100.0% | 100.0% |
| rule:preference | 10 | 100.0% | 100.0% | 100.0% |
| rule:procedure | 10 | 100.0% | 90.0% | 90.0% |
| state:baseline | 10 | 90.0% | 60.0% | 90.0% |
| state:status | 10 | 90.0% | 100.0% | 80.0% |
| state:uses | 10 | 100.0% | 100.0% | 100.0% |
| state:value | 10 | 80.0% | 80.0% | 80.0% |

## `typesafe/jev-1.14`: confusion (rows = generated label, columns = predicted) and reliability

| label \ predicted | state | plan | observation | rule | no answer |
|---|---|---|---|---|---|
| state | 36 | 3 | 0 | 1 | 0 |
| plan | 0 | 40 | 0 | 0 | 0 |
| observation | 0 | 0 | 40 | 0 | 0 |
| rule | 2 | 1 | 0 | 37 | 0 |

| top probability | n | mean confidence | accuracy |
|---|---|---|---|
| 0.4-0.5 | 2 | 0.45 | 0.0% |
| 0.5-0.6 | 6 | 0.56 | 100.0% |
| 0.6-0.7 | 4 | 0.66 | 50.0% |
| 0.7-0.8 | 6 | 0.76 | 66.7% |
| 0.8-0.9 | 7 | 0.85 | 100.0% |
| 0.9-1.0 | 135 | 0.98 | 99.3% |

## `liquid/d1.2-ultra`: confusion (rows = generated label, columns = predicted) and reliability

| label \ predicted | state | plan | observation | rule | no answer |
|---|---|---|---|---|---|
| state | 34 | 2 | 0 | 4 | 0 |
| plan | 0 | 40 | 0 | 0 | 0 |
| observation | 0 | 0 | 40 | 0 | 0 |
| rule | 1 | 1 | 0 | 38 | 0 |

| top probability | n | mean confidence | accuracy |
|---|---|---|---|
| 0.4-0.5 | 3 | 0.47 | 66.7% |
| 0.5-0.6 | 7 | 0.54 | 57.1% |
| 0.6-0.7 | 9 | 0.64 | 77.8% |
| 0.7-0.8 | 8 | 0.77 | 100.0% |
| 0.8-0.9 | 52 | 0.86 | 96.2% |
| 0.9-1.0 | 81 | 0.95 | 100.0% |

## `claude-haiku-5-5`: confusion (rows = generated label, columns = predicted) and reliability

| label \ predicted | state | plan | observation | rule | no answer |
|---|---|---|---|---|---|
| state | 35 | 4 | 0 | 1 | 0 |
| plan | 0 | 40 | 0 | 0 | 0 |
| observation | 0 | 0 | 40 | 0 | 0 |
| rule | 1 | 1 | 0 | 38 | 0 |

| top probability | n | mean confidence | accuracy |
|---|---|---|---|
| 0.5-0.6 | 6 | 0.53 | 50.0% |
| 0.6-0.7 | 19 | 0.63 | 84.2% |
| 0.7-0.8 | 24 | 0.73 | 95.8% |
| 0.8-0.9 | 80 | 0.83 | 100.0% |
| 0.9-1.0 | 31 | 0.92 | 100.0% |
