# Mnemo — MVP Recall Quality Sprint

## Status: READY TO BUILD
## Priority: Critical — three independent agents agree recall is low-signal
## Author: Tom + Claude (claude.ai), synthesising feedback from Clio, Nels's Claude, and Tom's Claude

---

## Problem Statement

Three agents dogfooding Mnemo independently converged on the same diagnosis:

- **Nels's Claude:** Stored 10 strategic points from a conversation. They split ~50/50 across episodic/semantic when all were arguably semantic. Average confidence 66%.
- **Tom's Claude (claude.ai):** Recall returns tangentially related atoms alongside relevant ones. Composite ranking (similarity × confidence) not yet active. Type classification adds metadata noise without improving retrieval.
- **Clio:** "The atoms I'm getting back feel low-signal — nothing's scored above 0.4 similarity, so I'm not sure what I'm supposed to do with them."
- **Tom's Claude Desktop (recall_shared test):** Core sharing thesis works — Nels's agent stored, shared a curated view, and Tom's agent searched it semantically with proper attribution. But duplicate episodic+semantic pairs eat the result budget (5 results → only 2-3 distinct pieces of information), similarity scores cap at 0.66 even on high-relevance queries, and `verbosity: "detailed"` returns a 422.

The consensus: Mnemo works mechanically (store/recall roundtrip is clean, cross-agent memory functions, and the sharing thesis is validated) but isn't yet making agents more useful. Recall quality IS the product.

---

## Root Cause Hypothesis

Two possible explanations for low similarity scores and noisy recall:

1. **Decomposition problem (upstream):** The regex decomposer produces atoms that don't correspond to coherent knowledge units, so embeddings are diffuse and similarity scores are low.
2. **Retrieval problem (downstream):** Atoms are fine but ranking, thresholds, or query formulation are suboptimal.

The sprint is ordered to disambiguate these two causes before committing to larger changes.

---

## Task 1: LLM Decomposer Spike (half day)

### Goal
Determine whether low similarity scores are caused by bad atoms (decomposition) or bad matching (retrieval).

### Method
Take the same 10 memories Nels stored via the batch chat-to-memory test. Run them through both:

1. The current regex decomposer
2. Haiku via the Anthropic API (claude-haiku-4-5-20251001)

For each, store the resulting atoms, then run the same recall queries and compare:
- Number of atoms produced
- Similarity scores on recall
- Subjective atom quality (does each atom represent one coherent claim?)
- Confidence scores assigned

### LLM Decomposer Prompt (starting point)

```
You are a memory decomposer. Given a block of text, extract discrete knowledge atoms.

Rules:
- Each atom should be ONE coherent claim, fact, or observation
- Preserve specificity — don't over-generalise
- Don't split tightly coupled facts into separate atoms
- If the text describes an event, capture the event as one atom
- If the text states a general fact, capture it as one atom
- Return JSON array of objects: {"text": "...", "confidence": 0.0-1.0}
- Confidence should reflect how certain/well-supported the claim is in the source text

Input text:
{text}
```

### Decision Criteria
- If LLM atoms produce recall similarity scores significantly higher (>0.1 delta on average), the problem is decomposition → proceed to replace regex decomposer with LLM version
- If scores are similar, the problem is downstream → focus on retrieval tuning
- Document results either way for the Mnemo Substack article

### Implementation Notes
- Use Haiku via Anthropic API for the spike. Decomposing 10 memories costs pennies.
- Use prompt caching: the decomposer system prompt is identical every call, only input text varies. Add `cache_control: {"type": "ephemeral"}` on the system message. Cache lives 5 min from last use.
- Tom uses `uv` for Python package management, not pip
- This is a spike — throwaway test script is fine, doesn't need to be production code

### Comparison Script

Don't write to the production database. Run both decomposers in a standalone script that embeds and scores locally.

```python
"""
decomposer_comparison.py — throwaway spike script
Compare regex vs Haiku decomposer on the same 10 memories.
Run: uv run decomposer_comparison.py
"""

import asyncio, json
from anthropic import AsyncAnthropic

DECOMPOSER_PROMPT = """..."""  # As defined above

# The 10 memories Nels stored from the batch chat-to-memory test
ORIGINAL_TEXTS = [
    # Paste the 10 original texts here
]

TEST_QUERIES = [
    "Mnemo architecture decisions",
    "agent-to-agent sharing",
    "confidence scoring",
    "decomposer design",
    "Beta distribution",
    # Add queries natural to the content
]

async def regex_decompose(text: str) -> list[dict]:
    """Call the existing regex decomposer (import from mnemo codebase)"""
    # from server.services.decomposer import decompose
    ...

async def haiku_decompose(text: str) -> list[dict]:
    """Call Haiku for decomposition"""
    client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[{
            "type": "text",
            "text": DECOMPOSER_PROMPT,
            "cache_control": {"type": "ephemeral"}
        }],
        messages=[{"role": "user", "content": text}]
    )
    return json.loads(response.content[0].text)

async def embed_and_score(atoms: list[dict], queries: list[str]) -> dict:
    """Embed atoms and queries, compute cosine similarity matrix"""
    # Use the same embedding model Mnemo uses in production
    # Return: {query: [(atom_text, similarity_score), ...]}
    ...

async def main():
    results = {"regex": {}, "haiku": {}}
    
    for label, decompose_fn in [("regex", regex_decompose), 
                                 ("haiku", haiku_decompose)]:
        all_atoms = []
        for text in ORIGINAL_TEXTS:
            atoms = await decompose_fn(text)
            all_atoms.extend(atoms)
        
        print(f"\n{'='*60}")
        print(f"{label.upper()}: {len(all_atoms)} atoms from {len(ORIGINAL_TEXTS)} inputs")
        for atom in all_atoms:
            print(f"  [{atom.get('confidence', '?')}] {atom['text'][:80]}...")
        
        scores = await embed_and_score(all_atoms, TEST_QUERIES)
        results[label] = {"atoms": all_atoms, "scores": scores}
    
    # Compare
    print(f"\n{'='*60}")
    print("COMPARISON")
    for query in TEST_QUERIES:
        regex_top = results["regex"]["scores"][query][0][1]
        haiku_top = results["haiku"]["scores"][query][0][1]
        delta = haiku_top - regex_top
        print(f"  {query}")
        print(f"    regex best: {regex_top:.3f}  haiku best: {haiku_top:.3f}  delta: {delta:+.3f}")

asyncio.run(main())
```

Eyeball the output. If haiku atoms are cleaner and similarity deltas are >0.1 on average, ship it.

### Schema Addition: `decomposer_version` Column

Add a `decomposer_version` text column to the atoms table. Values: `"regex_v1"`, `"haiku_v1"`. This costs one column, lets you query later to confirm Haiku atoms consistently score higher on recall, and gives you data for the Substack article.

```sql
ALTER TABLE atoms ADD COLUMN decomposer_version TEXT NOT NULL DEFAULT 'regex_v1';
```

When the Haiku decomposer ships, new atoms get tagged `"haiku_v1"`. Old atoms keep `"regex_v1"`. No migration, no reprocessing. Over time, better atoms naturally supplement the older ones.

### If the Spike Succeeds: Async Store Architecture

Storing is not on the critical path — recall is. An agent says "remember this" and moves on. Decomposition can happen in the background. This means cloud model latency is acceptable for decomposition even though it would be unacceptable for recall.

**Flow:**
1. Agent calls `mnemo_remember("some text")`
2. MCP server returns immediately with ack: `{"status": "queued", "store_id": "uuid"}`
3. Background worker picks up the job, calls Haiku for decomposition, stores resulting atoms
4. On success: atoms are stored, edges created, business as usual
5. On failure: original text + error + timestamp logged to `store_failures` table

**Why async is safe:** A failed store doesn't corrupt anything. The memory just doesn't exist. Next time the agent recalls, it's not there — same experience as if it was never stored. This is a missed write, not data corruption.

**Failure visibility:** Failures are an ops concern, not an agent concern. Log to a `store_failures` table scoped to the operator. Tom-the-operator sees a spike in failed stores, investigates. The agent doesn't need real-time notification. Explicitly do NOT build a callback/messaging layer for this — that's a message bus, not a memory system.

**Production decomposer config:** Hardcode Haiku, API key from `ANTHROPIC_API_KEY` env var. No config abstraction, no model selector, no Ollama fallback. The swappable backend (model selection, local LLM option, per-operator config) is a post-MVP feature that ships when the first external design partner tells you what they need to configure. Building it now would be guessing.

---

## Task 2: Composite Ranking (1 hour)

### Goal
When atoms are low-signal, at least surface the most confident ones first.

### Change
In `server/services/atom_service.py`, update the retrieval query ORDER BY:

```sql
-- Before:
ORDER BY similarity DESC

-- After:
ORDER BY (1 - (embedding <=> $query_embedding)) * effective_confidence DESC
```

Add `relevance_score` to the recall response so agents can see the composite score:

```python
relevance_score = similarity * effective_confidence
```

### Why Now
This is a one-line SQL change that directly addresses Clio's complaint. Even if atoms are noisy, ranking by similarity × confidence surfaces the best ones first. Low risk, immediate AX improvement.

---

## Task 3: Similarity Floor Tuning (30 min)

### Goal
Ensure the `min_similarity` floor (already specced at 0.3) is actually deployed and calibrated.

### Check
- Verify `min_similarity=0.3` is being passed in the MCP `recall` tool
- If Clio reports nothing above 0.4, then 0.3 is still letting through noise — consider raising to 0.35 after the LLM decomposer spike (if atom quality improves, similarity scores should rise and the floor can stay at 0.3)

### Note
Don't tune this until after Task 1. The decomposer spike may shift the similarity distribution upward, making the current floor appropriate.

---

## Task 4: Drop Type Classification from Retrieval Logic (30 min)

### Goal
Remove type-based filtering and ranking from the retrieval path. The semantic/episodic/procedural classification is not earning its keep.

### Changes
- Keep the `atom_type` field in the schema (costs nothing, useful later)
- Remove any retrieval logic that filters or weights by atom_type
- Remove atom_type from the MCP recall tool's parameters (agents shouldn't filter by type if classification is unreliable)
- Keep atom_type in the store path — still classify, just don't use it downstream

### Rationale
Misclassification (strategic knowledge tagged as episodic) creates invisible corruption. If retrieval ever treats types differently (different decay, different weighting), wrong labels actively distort what agents trust. Removing type from retrieval is a safety measure until classification quality improves.

### Bonus: This Fixes the Dedup Problem in recall_shared
Tom's Claude Desktop reported that duplicate episodic+semantic pairs consume the result budget — 5 results yield only 2-3 distinct pieces of information. This is a direct consequence of the decomposer creating near-identical atoms with different type labels. Dropping type from retrieval doesn't fix the duplicates at source, but Task 1 (LLM decomposer) should — an LLM won't produce the same text twice with different labels. In the meantime, consider adding a post-retrieval dedup pass that collapses atoms with >0.95 cosine similarity to each other, keeping the higher-confidence one.

---

## Separate Session: recall_shared Bug Fixes

The following issues were identified in `recall_shared` dogfooding but are **not part of this sprint**. They should be addressed in a dedicated coding session focused on the sharing path.

### Bug 1: 422 on `verbosity: "detailed"`
`recall_shared` rejects `verbosity: "detailed"` with a 422. Either the parameter isn't supported on the shared recall path or it validates against a different enum than regular `recall`. Fix: align the `recall_shared` endpoint to accept the same verbosity values as `recall`, or document the difference explicitly in the MCP tool schema.

### Bug 2: Similarity Scores Lower on Shared Recall
Even on queries that closely match the share description, similarity caps at 0.66. Investigate whether `recall_shared` uses the same embedding/similarity computation as regular `recall`. Possible causes: snapshot embeddings are stale (computed at share-time rather than query-time), or the shared view query path has a different similarity calculation.

### Enhancement: Browse All Shared Atoms
With 20 atoms in a shared view, 5 results per call plus duplicates means ~4 calls to explore the full set. Consider either: raising `max_results` ceiling for shared views, adding a `list_atoms` mode that returns all atoms in a shared view without similarity ranking (useful for the receiving agent to understand scope), or both.

### Enhancement: Dedup in Shared Recall Results
Even after Task 4 removes type from retrieval, shared views may still contain near-duplicate atoms from the original store. Add a dedup pass (>0.95 cosine similarity between result atoms → collapse to highest-confidence one) to `recall_shared` results. This directly improves the AX of receiving shared knowledge.

---

## What NOT To Build (Scope Boundaries for Claude Code)

Do not build any of the following in this sprint, even if they seem like natural extensions:

- **Fact-pinning / user-asserted confidence override** — enables overconfident opinion propagation via sharing
- **Episodic confidence Beta** — no principled update mechanism exists yet
- **Differential decay by type** — depends on reliable classification, which we're deprioritising
- **Swappable decomposer config** (model selector, Ollama fallback, per-operator settings) — no external operators exist yet
- **Agent failure callbacks** for async stores — operator logging is sufficient, don't build a message bus
- **RECALL_HEADER config toggle** — safety prefix stays non-configurable
- **Reprocessing old atoms** through the new decomposer — let them be naturally supplemented over time
- **A/B testing infrastructure** — the comparison script is the test; `decomposer_version` column is the long-term tracking

---

## Success Criteria

After this sprint:

1. You have data on whether the decomposer is the bottleneck (Task 1)
2. Recall results are ranked by composite relevance, not just similarity (Task 2)
3. Clio reports atoms that feel higher-signal in normal conversation (qualitative)
4. No retrieval logic depends on potentially-wrong type classification (Task 4)
5. Duplicate atom pairs no longer consume the result budget (Task 4 + dedup pass)

After the separate recall_shared session:

6. `verbosity: "detailed"` works on `recall_shared`
7. Receiving agents can browse the full scope of a shared view
8. Shared recall similarity scores are consistent with regular recall

---

## Build Order

1. Task 1: LLM decomposer spike (half day) — this determines everything else
2. Task 2: Composite ranking (1 hour)
3. Task 3: Similarity floor check (30 min, after Task 1 results)
4. Task 4: Drop type from retrieval (30 min)

Total: ~1 day

---

## AX Note

Tom coined "AX" (Agent Experience) as a framing for this work. The core AX question: when an agent recalls something, does the retrieved context help or add noise? This sprint is about making the answer "help" more often than "noise." Use in the first Mnemo Substack article.
