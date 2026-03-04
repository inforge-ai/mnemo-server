# Mnemo - Recall Quality Controls

## For: Claude Code / Implementation Agent
## Status: READY TO BUILD
## Priority: High - directly addresses +92% token overhead finding
## Prerequisite: Recall quality improvements (similarity floor, composite ranking) deployed
## Context: Financial skills test showed Mnemo wins 6/9 on quality but doubles token cost.
## The overhead comes from recall injecting too much context. These controls fix that.

---

## Background

Nine equity analyst tasks run in blind A/B: Mnemo won 6, tied 1, lost 2 (both losses
were truncation bugs in the agent runner, not quality losses). But Mnemo used 92% more
tokens (+92K). The overhead ranged from +2% (initiating-coverage) to +232% (idea-generation).

Analysis: the agent recalls memory before every task and injects all returned atoms
into context. With max_results=5 and graph expansion, each recall adds hundreds of
tokens of context, much of it marginally relevant. The quality improvement comes from
the 1-2 genuinely relevant atoms. The cost comes from the 3-4 irrelevant ones.

Three changes to fix this, plus the truncation bug fix.

---

## Change 1: Similarity Gap Threshold

**Problem:** Recall returns top N results regardless of whether results 3, 4, 5
are meaningfully similar to the query. With a small memory, everything above
min_similarity makes the cut. With a growing memory, low-relevance results still
fill the quota.

**Fix:** Add a `similarity_drop_threshold` parameter. After ranking results by
composite score, walk down the list. If the score drops by more than this
percentage from one result to the next, stop. Return only the results above
the cliff.

Example:
  Results ranked by score: [0.72, 0.68, 0.41, 0.38, 0.22]
  With similarity_drop_threshold=0.3 (30%):
    0.72 to 0.68: drop = 5.6% - keep
    0.68 to 0.41: drop = 39.7% - STOP
  Returns: [0.72, 0.68] instead of all 5.

In server/models.py, update RetrieveRequest:

    similarity_drop_threshold: float | None = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Stop returning results when score drops by this fraction"
    )

In the retrieval logic, after ranking by composite score:

    def apply_gap_threshold(atoms, threshold):
        if not threshold or len(atoms) <= 1:
            return atoms
        result = [atoms[0]]
        for i in range(1, len(atoms)):
            prev_score = atoms[i-1]["relevance_score"]
            curr_score = atoms[i]["relevance_score"]
            if prev_score > 0 and (prev_score - curr_score) / prev_score > threshold:
                break
            result.append(atoms[i])
        return result

Apply the same logic to expanded_atoms separately.

In client/mnemo_client.py, add the parameter to recall().

In the MCP recall tool, pass similarity_drop_threshold=0.3 as default.
Add as an optional tool parameter so callers can tune it.

**Tests:**

test_gap_threshold_stops_at_cliff:
  Store 5 atoms: 3 about Python pandas, 2 about medieval history.
  Recall "pandas CSV type coercion" with similarity_drop_threshold=0.3.
  Assert: returns only the pandas atoms (cliff between pandas and history).

test_gap_threshold_none_returns_all:
  Same setup. Recall with similarity_drop_threshold=None.
  Assert: returns all atoms up to max_results (current behaviour preserved).

test_gap_threshold_with_uniform_scores:
  Store 5 very similar atoms about the same topic.
  Recall with similarity_drop_threshold=0.3.
  Assert: returns all 5 (no cliff in scores).

test_gap_threshold_single_result:
  Store 1 relevant atom and 1 irrelevant atom.
  Recall with similarity_drop_threshold=0.3.
  Assert: returns only 1 atom if the drop is steep enough.

---

## Change 2: Recall Verbosity Control

**Problem:** Recall returns full text_content for every atom. An episodic arc
with 4 sentences or a detailed procedural memory adds hundreds of tokens to
context even when the agent only needs the gist.

**Fix:** Add a `verbosity` parameter to recall with three modes:

- "full" (default): return complete text_content (current behaviour)
- "summary": return first sentence only
- "truncated": return first N characters with ellipsis

In server/models.py, update RetrieveRequest:

    verbosity: str = Field(
        default="full",
        pattern="^(full|summary|truncated)$",
    )
    max_content_chars: int = Field(
        default=200,
        ge=50,
        le=5000,
        description="Character limit per atom when verbosity=truncated"
    )

In the retrieval logic, after fetching atoms but before returning:

    def apply_verbosity(atoms, verbosity, max_chars):
        if verbosity == "full":
            return atoms
        for atom in atoms:
            text = atom["text_content"]
            if verbosity == "summary":
                # First sentence
                end = text.find(". ")
                if end > 0:
                    atom["text_content"] = text[:end + 1]
                # else keep full text (single sentence)
            elif verbosity == "truncated":
                if len(text) > max_chars:
                    atom["text_content"] = text[:max_chars].rstrip() + "..."
        return atoms

Apply to both primary atoms and expanded_atoms.

In the MCP recall tool, default to "summary" (not "full") for token efficiency.
The agent gets the gist. If it needs full detail on a specific atom, it can
recall again with a targeted query and verbosity="full".

Update MCP tool description:

    "Search memories. Returns first-sentence summaries by default.
     Set verbosity to 'full' for complete content."

In client/mnemo_client.py, add verbosity and max_content_chars to recall().

**Tests:**

test_verbosity_full_returns_complete_text:
  Store a 3-sentence memory.
  Recall with verbosity="full".
  Assert: text_content is the complete text.

test_verbosity_summary_returns_first_sentence:
  Store "First sentence here. Second sentence here. Third sentence."
  Recall with verbosity="summary".
  Assert: text_content is "First sentence here."

test_verbosity_truncated_respects_char_limit:
  Store a 500-character memory.
  Recall with verbosity="truncated", max_content_chars=100.
  Assert: text_content is 103 characters (100 + "...")

test_verbosity_summary_single_sentence:
  Store "Only one sentence no period"
  Recall with verbosity="summary".
  Assert: returns full text (no sentence boundary to split on).

---

## Change 3: Recall Token Budget

**Problem:** The caller has no way to say "give me at most N tokens of context."
With 5 atoms each containing 50-200 tokens of text, plus expanded atoms, a
single recall can inject 500-1500 tokens into the agent's context window.

**Fix:** Add a `max_total_tokens` parameter. After ranking and gap-threshold
filtering, accumulate results until the token budget is exhausted. Estimate
tokens as character_count / 4 (rough approximation, good enough for budgeting).

In server/models.py, update RetrieveRequest:

    max_total_tokens: int | None = Field(
        default=None,
        ge=50,
        le=10000,
        description="Approximate token budget for all returned content"
    )

In the retrieval logic, after gap threshold but before verbosity:

    def apply_token_budget(atoms, max_tokens):
        if max_tokens is None:
            return atoms
        budget = max_tokens
        result = []
        for atom in atoms:
            estimated_tokens = len(atom["text_content"]) / 4
            if budget - estimated_tokens < 0 and len(result) > 0:
                break
            budget -= estimated_tokens
            result.append(atom)
        return result

Always include at least 1 result (the most relevant) even if it exceeds
the budget. Apply before verbosity so that truncation can further reduce
token usage.

Apply the budget to primary atoms first, then allocate remaining budget
to expanded_atoms. If primary atoms exhaust the budget, return no
expanded atoms.

In the MCP recall tool, set a default budget:

    max_total_tokens=500  # reasonable default for context injection

In client/mnemo_client.py, add max_total_tokens to recall().

**Tests:**

test_token_budget_limits_results:
  Store 5 atoms each with ~100 words (~130 tokens).
  Recall with max_total_tokens=300.
  Assert: returns 2-3 atoms (not all 5).

test_token_budget_always_returns_one:
  Store 1 atom with 500 words.
  Recall with max_total_tokens=50.
  Assert: returns 1 atom (always at least the most relevant).

test_token_budget_none_returns_all:
  Store 5 atoms. Recall with max_total_tokens=None.
  Assert: returns all (current behaviour preserved).

test_token_budget_applies_to_expanded_separately:
  Store atoms with graph edges.
  Recall with max_total_tokens=300.
  Assert: if primary atoms use 250 tokens, expanded atoms get
  at most 50 tokens of budget.

---

## Change 4: Fix Agent Runner Truncation Bug

**Problem:** In the financial skills test, thesis-tracker produced 75 words
and model-update cut off mid-sentence. Both appear to be the agent spending
too many turns on Mnemo tool calls and either hitting a turn limit or the
runner mishandling a response that ends with a tool call.

**Fix:** This is in the agent runner (fin-agent), not in Mnemo itself.
Investigate and fix:

1. Check whether the runner has a max_turns limit. If the agent makes
   7 tool calls (recalls + remembers + web searches), it might exhaust
   its turns before producing the final text response.
   Fix: increase max_turns, or don't count tool calls toward the limit.

2. Check whether the runner handles a final response that is a tool_use
   block without a subsequent text block. If the agent's last message is
   a tool call, the runner might terminate without giving the agent a
   chance to produce the final text.
   Fix: after every tool result, give the agent another turn. Only
   terminate when the agent produces an end_turn stop_reason.

3. Check whether the agent is getting into a recall loop: recall,
   get results, remember something about the results, recall again.
   The gap threshold and token budget will help prevent this, but
   also check whether the system prompt tells the agent to recall
   once at the start and remember once at the end, not continuously.

**Verification:** After fixing, rerun thesis-tracker and model-update only.
Both should produce full outputs. Rescore blind against vanilla.

---

## Change 5: Update MCP Tool Defaults

After implementing Changes 1-3, update the MCP recall tool defaults to
be token-efficient out of the box:

    result = await client.recall(
        agent_id=target,
        query=query,
        domain_tags=domain_tags,
        max_results=max_results or 5,
        min_confidence=0.1,
        min_similarity=0.3,
        similarity_drop_threshold=0.3,     # NEW: stop at cliff
        verbosity="summary",               # NEW: first sentence only
        max_total_tokens=500,              # NEW: cap context injection
        expand_graph=True,
    )

These defaults mean a typical recall returns 1-3 atoms, first sentence
only, within a 500 token budget. The agent gets focused, relevant context
without flooding. Any of these can be overridden per call.

---

## Pipeline Order

The three controls apply in sequence:

1. Retrieve candidates (existing: similarity floor, composite ranking)
2. Apply gap threshold (Change 1) - reduces result count
3. Apply token budget (Change 3) - further reduces if still too many
4. Apply verbosity (Change 2) - reduces per-atom token cost
5. Return to caller

This ordering means: first cut irrelevant results, then cut excess
results, then compress what remains.

---

## Build Order

1. Change 1: Gap threshold (~45 min)
   - Add parameter, implement logic, add tests
   - pytest tests/test_api.py -v

2. Change 2: Verbosity control (~30 min)
   - Add parameter, implement logic, add tests
   - pytest tests/test_api.py -v

3. Change 3: Token budget (~30 min)
   - Add parameter, implement logic, add tests
   - pytest tests/test_api.py -v

4. Change 5: Update MCP defaults (~10 min)
   - Update recall tool defaults
   - Update tool description

5. Full regression: pytest tests/ -v

6. Change 4: Fix runner truncation (separate from Mnemo)
   - Investigate and fix fin-agent runner
   - Rerun 2 failed tasks

Total Mnemo changes: ~2 hours
Runner fix: ~30 min (separate codebase)

---

## How to Verify

Rerun the 9 financial skills tasks with the new defaults. Expected:

- Token overhead drops from +92% to +30-50%
- Quality wins preserved (6+ out of 9 in blind evaluation)
- Truncation failures eliminated (thesis-tracker and model-update produce full outputs)
- Total cost closer to 1.2-1.4x vanilla instead of 1.9x

If token overhead drops below +30% with quality preserved, the product
story becomes: "Mnemo adds modest cost for a step change in output quality."
That is a strong value proposition.

If quality drops when recall is tightened, the extra context WAS doing
useful work and the cost is justified. That is also a valid finding —
it means the token overhead is the price of quality, not waste.
