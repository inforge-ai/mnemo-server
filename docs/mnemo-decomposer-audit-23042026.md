# Mnemo Decomposer Classification Audit (Ticket 4a)

**Date:** 2026-04-23
**Phase 0 deliverable** for `docs/mnemo-iteration-23042026.md`, Ticket 4a.

## What changed

`mnemo/server/llm_decomposer.py`:

1. Replaced the `Types` section of `DECOMPOSER_PROMPT` with an expanded episodic definition covering plans, decisions, intentions, and "as of" state claims, plus a rule-of-thumb ("if the truth of the claim could change by the time someone reads it back, the atom is episodic"). Reciprocally tightened the semantic definition to emphasise *timeless* and added a contrastive rule ("if substituting is/was/will-be changes the meaning, it is NOT semantic").
2. Added `_looks_like_state_claim()` — a narrow post-processing backstop matching five pattern families (`is planned|scheduled|being planned|in progress`, `is currently|now|the current|the active|the next`, `(has|have) not (yet )?been`, `is on the roadmap`, `are planning to`). Applied only to atoms the LLM tagged as `semantic`; downgrades to `episodic` on match.

Backstop is intentionally narrow. False negatives (state claims the LLM gets right in the prompt) are fine; false positives (timeless facts accidentally flipped to episodic) are costly because they would inherit the 14-day episodic half-life.

## Acceptance evidence

**Seeded tests** — `tests/test_llm_decomposer.py`:

- `TestStateClaimBackstop` covers the Zulip motivating case, "is currently", "has not yet been", timeless semantic untouched, episodic-from-LLM untouched, procedural-with-state-language untouched.
- `TestStateClaimPatterns` exercises `_looks_like_state_claim` directly for each pattern family, plus negative cases (plain facts, past events).

The test suite requires `mnemo_test` PostgreSQL DB (via `MNEMO_TEST_DATABASE_URL`), which is not currently provisioned on `inforge-ops`. The pure-logic portions of both test classes were verified via direct Python invocation at Phase 0:

```
OK — all 11 backstop assertions pass
OK 1: Zulip state claim downgraded to episodic
OK 2: timeless semantic stays semantic
OK 3: episodic from LLM unchanged
OK 4: procedural with state language stays procedural
```

**100-atom production audit** — 100 semantic atoms sampled uniformly from `mnemo` on `inforge-ops` (active only, `ORDER BY md5(id::text) LIMIT 100`). Manual review:

- ~13 clear state-claim misclassifications that should have been episodic — e.g. *"Tom is currently in therapy"*, *"Calvin is on tour with Frank Ocean"*, *"John is trying to socialize more"*, *"Scenario 3 under consideration"*, *"Lloyd implementation is planned"*, *"Mnemo Edge is planned as a v1.1 feature"*.
- ~4 borderline atoms (config-state that drifts but is commonly treated as a stable fact: *"Operating system is Ubuntu 24.04"*, email config, historical identifications).
- ~83 correctly-classified semantic atoms (durable preferences, opinions, technical facts, biographical claims).

Baseline state-claim misclassification rate against live production data: ~13%.

Sweeping the full active-semantic population (`n = 4,617`) with the backstop's ILIKE-equivalent patterns flags 18 atoms — all confirmed state/plan claims on inspection. That is the subset the narrow backstop catches directly; the remaining ~10% of the misclassification class relies on the prompt change, which can only be measured by re-decomposing, which is deferred until the iteration rollout.

## Out of scope for Ticket 4a

- **No recency ranking within episodic matches.** That is Ticket 4b, blocked until Phase 2. Nothing in this change touches retrieval.
- **No retroactive re-classification.** Existing mis-typed atoms stay as they are. Store self-heals forward as new episodic atoms supersede stale semantic ones by recency.
- **No changes to the regex decomposer (`decomposer.py`).** It remains the LLM-error fallback, unchanged.
- **No ambiguous-case entity test** (from the Review notes). That is part of Ticket 3 when picked up in Phase 1.
