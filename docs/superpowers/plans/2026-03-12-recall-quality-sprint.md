# Recall Quality Sprint Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve recall quality by replacing the regex decomposer with an LLM decomposer, adding composite ranking, and removing type-based retrieval noise.

**Architecture:** Four sequential tasks. Task 1 (LLM decomposer spike) is a standalone comparison script that determines whether bad atoms or bad retrieval cause low similarity. If the spike succeeds, the LLM decomposer ships as the production decomposer (synchronous — latency is acceptable on the store path). Tasks 2-4 are surgical changes to retrieval ranking, similarity floor, and type filtering.

**Tech Stack:** Anthropic API (claude-haiku-4-5-20251001, with prompt caching), asyncpg, sentence-transformers (all-MiniLM-L6-v2), FastAPI, pytest

**Spec:** `docs/mnemo_mvp_recall_spec.md`

---

## File Structure

### New files
- `decomposer_comparison.py` — throwaway spike script (Task 1), lives at project root
- `mnemo/server/llm_decomposer.py` — LLM decomposer module (if spike succeeds)
- `tests/test_llm_decomposer.py` — tests for LLM decomposer
- `tests/test_recall_ranking.py` — tests for composite ranking and dedup

### Modified files
- `mnemo/server/services/atom_service.py` — wire LLM decomposer, post-retrieval dedup, remove atom_type filter from retrieval and dedup
- `mnemo/server/services/view_service.py` — composite ranking in recall_shared/recall_all_shared
- `mnemo/server/models.py` — remove `atom_types` from `RetrieveRequest`
- `mnemo/server/routes/memory.py` — stop passing atom_types
- `schema.sql` — add `decomposer_version` column, `store_failures` table
- `tests/conftest.py` — add store_failures to cleanup

### NOT creating (deferred per spec)
- `mnemo/server/store_worker.py` — async background worker is a future optimization. The synchronous store path with LLM decomposer is sufficient (spec: "cloud model latency is acceptable for decomposition").

---

## Chunk 1: Task 1 — LLM Decomposer Spike

### Task 1.1: Comparison Script

This is a throwaway spike script. No TDD — just write it, run it, read the output.

**Files:**
- Create: `decomposer_comparison.py`

- [ ] **Step 1: Write the comparison script**

```python
"""
decomposer_comparison.py — throwaway spike script
Compare regex vs Haiku decomposer on the same memories.
Run: uv run python decomposer_comparison.py
Requires: ANTHROPIC_API_KEY env var
"""

import asyncio
import json
import numpy as np
from anthropic import AsyncAnthropic

# Import the existing regex decomposer
from mnemo.server.decomposer import decompose

DECOMPOSER_PROMPT = """You are a memory decomposer. Given a block of text, extract discrete knowledge atoms.

Rules:
- Each atom should be ONE coherent claim, fact, or observation
- Preserve specificity — don't over-generalise
- Don't split tightly coupled facts into separate atoms
- If the text describes an event, capture the event as one atom
- If the text states a general fact, capture it as one atom
- Return JSON array of objects: {"text": "...", "confidence": 0.0-1.0}
- Confidence should reflect how certain/well-supported the claim is in the source text

Return ONLY the JSON array, no other text."""

# Representative test memories — mix of strategic, episodic, procedural content
ORIGINAL_TEXTS = [
    "Mnemo uses Beta distributions for confidence scoring. Each atom has alpha and beta parameters that represent evidence for and against. The expected confidence is alpha/(alpha+beta). This was confirmed working in testing.",
    "I discovered that the decomposer splits sentences on periods, which breaks dotted identifiers like pd.read_csv and torch.nn.Module. The regex protects inline code blocks but not bare identifiers in prose.",
    "When deploying Mnemo, always use pgvector with the ivfflat index type. The cosine distance operator is <=> in PostgreSQL. Make sure to create the vector extension before running the schema.",
    "Agent-to-agent sharing works through views and capabilities. A view is a filtered snapshot of an agent's memory. Capabilities grant read access to specific views. The receiving agent can search the shared view semantically.",
    "I think the consolidation interval might be too aggressive at 60 minutes. Atoms that are accessed frequently should decay slower. The access_bonus in the decay function uses ln(1 + access_count) * 0.1 as a multiplier.",
    "The graph expansion uses a recursive CTE to walk edges up to N hops from seed atoms. Each hop multiplies the relevance score by edge_weight * 0.7. This means atoms 3 hops away have at most 0.343 of the original relevance.",
    "Yesterday I tested the recall endpoint with 50 stored atoms. Similarity scores capped at around 0.65 even for queries that closely matched stored content. The embedding model is all-MiniLM-L6-v2 with 384 dimensions.",
    "Best practice: when storing procedural knowledge, use imperative language like 'always do X' or 'never do Y'. The decomposer recognizes these patterns and assigns higher confidence via Beta(8,1). Hedging language like 'maybe' or 'I think' gets lower confidence.",
    "The snapshot_atoms table freezes the set of atom IDs at view creation time. This means shared views are scope-safe — graph expansion cannot pull atoms outside the snapshot. However, atoms can still decay below visibility within the snapshot.",
    "I found that duplicate detection only compares atoms of the same type. If the decomposer creates an episodic and a semantic atom with near-identical text, they won't be caught as duplicates. This wastes the result budget on recall.",
]

TEST_QUERIES = [
    "How does confidence scoring work in Mnemo?",
    "What are the issues with the decomposer?",
    "How does agent-to-agent sharing work?",
    "What is the graph expansion algorithm?",
    "How does decay affect memory retrieval?",
    "What are best practices for storing memories?",
    "How do embeddings and similarity search work?",
    "What problems were found during testing?",
]


def regex_decompose(text: str) -> list[dict]:
    """Call the existing regex decomposer."""
    atoms = decompose(text)
    return [{"text": a.text, "confidence": a.confidence_alpha / (a.confidence_alpha + a.confidence_beta)} for a in atoms]


async def haiku_decompose(client: AsyncAnthropic, text: str) -> list[dict]:
    """Call Haiku for decomposition with prompt caching."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[{
            "type": "text",
            "text": DECOMPOSER_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": text}],
    )
    return json.loads(response.content[0].text)


def embed_texts(texts: list[str]) -> np.ndarray:
    """Embed a list of texts using the same model Mnemo uses."""
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2")
    return model.encode(texts, normalize_embeddings=True)


async def main():
    client = AsyncAnthropic()
    results = {}

    for label in ["regex", "haiku"]:
        all_atoms = []
        for text in ORIGINAL_TEXTS:
            if label == "regex":
                atoms = regex_decompose(text)
            else:
                atoms = await haiku_decompose(client, text)
            all_atoms.extend(atoms)

        print(f"\n{'=' * 70}")
        print(f"{label.upper()}: {len(all_atoms)} atoms from {len(ORIGINAL_TEXTS)} inputs")
        print(f"{'=' * 70}")
        for i, atom in enumerate(all_atoms):
            conf = atom.get("confidence", 0.5)
            text_preview = atom["text"][:100]
            print(f"  [{i+1:2d}] conf={conf:.2f}  {text_preview}{'...' if len(atom['text']) > 100 else ''}")

        # Embed atoms and queries
        atom_texts = [a["text"] for a in all_atoms]
        all_texts = atom_texts + TEST_QUERIES
        embeddings = embed_texts(all_texts)
        atom_embeddings = embeddings[:len(atom_texts)]
        query_embeddings = embeddings[len(atom_texts):]

        sim_matrix = query_embeddings @ atom_embeddings.T

        scores = {}
        for qi, query in enumerate(TEST_QUERIES):
            ranked = sorted(
                [(atom_texts[ai], float(sim_matrix[qi, ai])) for ai in range(len(atom_texts))],
                key=lambda x: x[1],
                reverse=True,
            )
            scores[query] = ranked

        results[label] = {"atoms": all_atoms, "scores": scores}

    # Comparison
    print(f"\n{'=' * 70}")
    print("COMPARISON: Top similarity scores per query")
    print(f"{'=' * 70}")
    deltas = []
    for query in TEST_QUERIES:
        regex_top = results["regex"]["scores"][query][0][1]
        haiku_top = results["haiku"]["scores"][query][0][1]
        delta = haiku_top - regex_top
        deltas.append(delta)
        winner = "HAIKU" if delta > 0.01 else ("REGEX" if delta < -0.01 else "TIE")
        print(f"\n  Q: {query}")
        print(f"    regex best: {regex_top:.4f}  |  haiku best: {haiku_top:.4f}  |  delta: {delta:+.4f}  [{winner}]")
        print(f"    regex top atom: {results['regex']['scores'][query][0][0][:80]}...")
        print(f"    haiku top atom: {results['haiku']['scores'][query][0][0][:80]}...")

    avg_delta = sum(deltas) / len(deltas)
    print(f"\n{'=' * 70}")
    print(f"AVERAGE DELTA: {avg_delta:+.4f}")
    if avg_delta > 0.1:
        print("VERDICT: LLM decomposer is significantly better. Ship it.")
    elif avg_delta > 0.05:
        print("VERDICT: LLM decomposer is moderately better. Likely worth shipping.")
    elif avg_delta > 0:
        print("VERDICT: LLM decomposer is marginally better. Problem may be downstream.")
    else:
        print("VERDICT: No improvement. Problem is downstream, not decomposition.")
    print(f"{'=' * 70}")


asyncio.run(main())
```

- [ ] **Step 2: Run the comparison script**

```bash
uv run python decomposer_comparison.py
```

Review the output. The key metric is the average delta between haiku and regex top similarity scores.

**Decision point:**
- If average delta > 0.05: proceed with LLM decomposer (Tasks 1.2-1.4)
- If average delta <= 0.05: skip Tasks 1.2-1.4, focus on Tasks 2-4 (retrieval tuning)
- Either way: save the output for documentation

- [ ] **Step 3: Save the output**

Copy the terminal output to `docs/decomposer_spike_results.txt` for the Substack article.

- [ ] **Step 4: Commit**

```bash
git add decomposer_comparison.py docs/decomposer_spike_results.txt
git commit -m "spike: compare regex vs haiku decomposer on recall quality"
```

---

### Task 1.2: Schema Changes

**Files:**
- Modify: `schema.sql` (atoms table + new store_failures table)
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add `decomposer_version` column to atoms table in schema.sql**

In the `CREATE TABLE atoms` block, add after `last_consolidated_at TIMESTAMPTZ`:

```sql
    -- Decomposer provenance
    decomposer_version TEXT NOT NULL DEFAULT 'regex_v1',
```

- [ ] **Step 2: Add `store_failures` table to schema.sql**

Add after the `operations` table and its indexes:

```sql
-- Failed async store jobs (ops visibility, not agent-facing)
CREATE TABLE store_failures (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id    UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    original_text TEXT NOT NULL,
    error       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_store_failures_agent ON store_failures (agent_id, created_at);

GRANT SELECT, INSERT, DELETE ON store_failures TO mnemo;
```

- [ ] **Step 3: Run migrations on both databases**

```bash
sudo -u postgres psql mnemo -c "ALTER TABLE atoms ADD COLUMN IF NOT EXISTS decomposer_version TEXT NOT NULL DEFAULT 'regex_v1';"
sudo -u postgres psql mnemo_test -c "ALTER TABLE atoms ADD COLUMN IF NOT EXISTS decomposer_version TEXT NOT NULL DEFAULT 'regex_v1';"
sudo -u postgres psql mnemo -c "
CREATE TABLE IF NOT EXISTS store_failures (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    original_text TEXT NOT NULL,
    error TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_store_failures_agent ON store_failures (agent_id, created_at);
GRANT SELECT, INSERT, DELETE ON store_failures TO mnemo;
"
sudo -u postgres psql mnemo_test -c "
CREATE TABLE IF NOT EXISTS store_failures (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    original_text TEXT NOT NULL,
    error TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_store_failures_agent ON store_failures (agent_id, created_at);
GRANT SELECT, INSERT, DELETE ON store_failures TO mnemo;
"
```

- [ ] **Step 4: Add store_failures to conftest.py cleanup**

In `tests/conftest.py`, add `DELETE FROM store_failures;` to the `_CLEAN` string, before `DELETE FROM atoms;`:

```python
_CLEAN = """
DELETE FROM capabilities;
DELETE FROM snapshot_atoms;
DELETE FROM edges;
DELETE FROM views;
DELETE FROM store_failures;
DELETE FROM atoms;
DELETE FROM api_keys;
DELETE FROM agent_addresses;
DELETE FROM agents;
DELETE FROM operations;
DELETE FROM operators;
"""
```

- [ ] **Step 5: Add `related` to edges CHECK constraint**

The similarity-based edge pass (Task 1.4) needs a `"related"` edge type. Add it to the edges table CHECK constraint in `schema.sql`:

```sql
    edge_type       TEXT NOT NULL CHECK (edge_type IN (
                        'supports', 'contradicts', 'depends_on',
                        'generalises', 'specialises', 'motivated_by',
                        'evidence_for', 'supersedes', 'summarises', 'related'
                    )),
```

Also add it to the `EdgeCreate` model in `models.py`:
```python
    edge_type: Literal[
        "supports", "contradicts", "depends_on", "generalises",
        "specialises", "motivated_by", "evidence_for", "supersedes", "summarises", "related"
    ]
```

Run the migration:
```bash
sudo -u postgres psql mnemo -c "ALTER TABLE edges DROP CONSTRAINT IF EXISTS edges_edge_type_check; ALTER TABLE edges ADD CONSTRAINT edges_edge_type_check CHECK (edge_type IN ('supports', 'contradicts', 'depends_on', 'generalises', 'specialises', 'motivated_by', 'evidence_for', 'supersedes', 'summarises', 'related'));"
sudo -u postgres psql mnemo_test -c "ALTER TABLE edges DROP CONSTRAINT IF EXISTS edges_edge_type_check; ALTER TABLE edges ADD CONSTRAINT edges_edge_type_check CHECK (edge_type IN ('supports', 'contradicts', 'depends_on', 'generalises', 'specialises', 'motivated_by', 'evidence_for', 'supersedes', 'summarises', 'related'));"
```

- [ ] **Step 6: Commit**

```bash
git add schema.sql mnemo/server/models.py tests/conftest.py
git commit -m "schema: add decomposer_version column, store_failures table, related edge type"
```

---

### Task 1.3: LLM Decomposer Module

**Files:**
- Create: `mnemo/server/llm_decomposer.py`
- Create: `tests/test_llm_decomposer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_decomposer.py
"""Tests for the LLM decomposer."""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from mnemo.server.llm_decomposer import llm_decompose


class TestLLMDecomposer:
    """Unit tests for llm_decompose — no API calls, mocked Anthropic client."""

    @pytest.mark.asyncio
    async def test_basic_decomposition(self):
        """LLM decomposer returns DecomposedAtom list from API response."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Mnemo uses Beta distributions for confidence", "confidence": 0.9},
            {"text": "Expected confidence is alpha/(alpha+beta)", "confidence": 0.85},
        ]))]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            atoms = await llm_decompose("Mnemo uses Beta distributions for confidence. Expected confidence is alpha/(alpha+beta).")

        assert len(atoms) == 2
        assert atoms[0].text == "Mnemo uses Beta distributions for confidence"
        assert atoms[0].atom_type == "semantic"
        assert atoms[0].confidence_alpha == 8.0  # >= 0.8 → Beta(8,1)
        assert atoms[0].confidence_beta == 1.0
        assert atoms[1].text == "Expected confidence is alpha/(alpha+beta)"

    @pytest.mark.asyncio
    async def test_confidence_mapping_high(self):
        """Confidence >= 0.8 maps to Beta(8, 1)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "This is certain", "confidence": 0.95},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            atoms = await llm_decompose("This is certain")

        assert atoms[0].confidence_alpha == 8.0
        assert atoms[0].confidence_beta == 1.0

    @pytest.mark.asyncio
    async def test_confidence_mapping_moderate(self):
        """Confidence 0.6-0.8 maps to Beta(4, 2)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "A known fact", "confidence": 0.7},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            atoms = await llm_decompose("A known fact")

        assert atoms[0].confidence_alpha == 4.0
        assert atoms[0].confidence_beta == 2.0

    @pytest.mark.asyncio
    async def test_confidence_mapping_low(self):
        """Confidence 0.25-0.4 maps to Beta(2, 3)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Maybe this is true", "confidence": 0.3},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            atoms = await llm_decompose("Maybe this is true")

        assert atoms[0].confidence_alpha == 2.0
        assert atoms[0].confidence_beta == 3.0

    @pytest.mark.asyncio
    async def test_confidence_mapping_very_low(self):
        """Confidence < 0.25 maps to Beta(2, 4)."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "I have no idea if this is right", "confidence": 0.15},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            atoms = await llm_decompose("I have no idea if this is right")

        assert atoms[0].confidence_alpha == 2.0
        assert atoms[0].confidence_beta == 4.0

    @pytest.mark.asyncio
    async def test_empty_input(self):
        """Empty or whitespace input returns empty list without API call."""
        mock_client = AsyncMock()

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            atoms = await llm_decompose("")

        assert atoms == []
        mock_client.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_error_raises(self):
        """API errors propagate — caller handles them."""
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            with pytest.raises(Exception, match="API error"):
                await llm_decompose("Some text")

    @pytest.mark.asyncio
    async def test_uses_prompt_caching(self):
        """System prompt uses cache_control for Anthropic prompt caching."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "test", "confidence": 0.5},
        ]))]
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            await llm_decompose("test text")

        call_kwargs = mock_client.messages.create.call_args[1]
        system_msg = call_kwargs["system"][0]
        assert system_msg["cache_control"] == {"type": "ephemeral"}
        assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_llm_decomposer.py -v
```

Expected: FAIL — `mnemo.server.llm_decomposer` does not exist.

- [ ] **Step 3: Write the LLM decomposer module**

```python
# mnemo/server/llm_decomposer.py
"""
LLM-based decomposer using Anthropic Haiku with prompt caching.

Replaces the regex decomposer for higher-quality atom extraction.
Confidence is inferred by the LLM and mapped to Beta distribution parameters.

Prompt caching: The system prompt is marked with cache_control=ephemeral so
identical system prompts within a 5-minute window are served from cache.
"""

import json
import logging
from functools import lru_cache

from anthropic import AsyncAnthropic

from .decomposer import DecomposedAtom

logger = logging.getLogger(__name__)

DECOMPOSER_PROMPT = """You are a memory decomposer. Given a block of text, extract discrete knowledge atoms.

Rules:
- Each atom should be ONE coherent claim, fact, or observation
- Preserve specificity — don't over-generalise
- Don't split tightly coupled facts into separate atoms
- If the text describes an event, capture the event as one atom
- If the text states a general fact, capture it as one atom
- Return JSON array of objects: {"text": "...", "confidence": 0.0-1.0}
- Confidence should reflect how certain/well-supported the claim is in the source text

Return ONLY the JSON array, no other text."""

MODEL = "claude-haiku-4-5-20251001"


@lru_cache(maxsize=1)
def _get_client() -> AsyncAnthropic:
    """Singleton Anthropic client. Reads ANTHROPIC_API_KEY from env."""
    return AsyncAnthropic()


def _confidence_to_beta(confidence: float) -> tuple[float, float]:
    """Map LLM-assigned confidence [0,1] to Beta distribution parameters.

    Bands match the regex decomposer's output so decay behaviour is consistent:
      >= 0.8  -> Beta(8, 1)   high confidence
      >= 0.6  -> Beta(4, 2)   moderate
      >= 0.4  -> Beta(3, 2)   mild
      >= 0.25 -> Beta(2, 3)   low
      <  0.25 -> Beta(2, 4)   very low
    """
    if confidence >= 0.8:
        return (8.0, 1.0)
    elif confidence >= 0.6:
        return (4.0, 2.0)
    elif confidence >= 0.4:
        return (3.0, 2.0)
    elif confidence >= 0.25:
        return (2.0, 3.0)
    else:
        return (2.0, 4.0)


async def llm_decompose(text: str) -> list[DecomposedAtom]:
    """Decompose text into atoms using Haiku with prompt caching.

    Returns DecomposedAtom list compatible with the existing store pipeline.
    All atoms are typed 'semantic' — the LLM focuses on content quality,
    not type classification (which Task 4 removes from retrieval anyway).

    Note: Since all atoms are 'semantic', infer_edges() will produce no
    edges (it needs type diversity for episodic->semantic etc links).
    This is acceptable — the LLM produces cleaner atoms that don't need
    type-based edge inference.
    """
    if not text or not text.strip():
        return []

    client = _get_client()
    response = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": DECOMPOSER_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": text}],
    )

    raw = json.loads(response.content[0].text)
    atoms = []
    for item in raw:
        alpha, beta = _confidence_to_beta(item.get("confidence", 0.5))
        atoms.append(DecomposedAtom(
            text=item["text"],
            atom_type="semantic",
            confidence_alpha=alpha,
            confidence_beta=beta,
            source_type="direct_experience",
        ))

    return atoms
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_llm_decomposer.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add mnemo/server/llm_decomposer.py tests/test_llm_decomposer.py
git commit -m "feat: add LLM decomposer module using Anthropic Haiku with prompt caching"
```

---

### Task 1.4: Wire LLM Decomposer into Store Path

**Files:**
- Modify: `mnemo/server/services/atom_service.py` — swap decomposer, add decomposer_version to INSERT

- [ ] **Step 1: Write failing test**

Add to `tests/test_llm_decomposer.py`:

```python
class TestDecomposerIntegration:
    """Test that the correct decomposer is selected based on ANTHROPIC_API_KEY."""

    @pytest.mark.asyncio
    async def test_falls_back_to_regex_without_api_key(self):
        """Without ANTHROPIC_API_KEY, _decompose uses the regex decomposer."""
        from mnemo.server.services.atom_service import _decompose
        import os

        # Ensure no API key
        key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            atoms = await _decompose("The sky is blue.")
            assert len(atoms) >= 1
            # Regex decomposer returns DecomposedAtom objects
            assert hasattr(atoms[0], "text")
            assert hasattr(atoms[0], "atom_type")
        finally:
            if key:
                os.environ["ANTHROPIC_API_KEY"] = key
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_llm_decomposer.py::TestDecomposerIntegration -v
```

Expected: FAIL — `_decompose` doesn't exist yet.

- [ ] **Step 3: Add `_decompose` helper and update `store_from_text`**

In `mnemo/server/services/atom_service.py`:

1. Add import at top:
```python
import os
```

2. Change the decomposer import:
```python
from ..decomposer import decompose as regex_decompose, infer_edges, DecomposedAtom
```

3. Add the `_decompose` helper after the `HALF_LIVES` dict:
```python
async def _decompose(text: str, domain_tags: list[str] | None = None) -> list[DecomposedAtom]:
    """Use LLM decomposer if ANTHROPIC_API_KEY is set, else fall back to regex."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        from ..llm_decomposer import llm_decompose
        return await llm_decompose(text)
    return regex_decompose(text, domain_tags)
```

4. In `store_from_text` (around line 263), change:
```python
decomposed = decompose(text, domain_tags)
```
to:
```python
decomposed = await _decompose(text, domain_tags)
```

5. Update `_insert_atom` to include `decomposer_version` in the INSERT. Change the INSERT SQL to add `decomposer_version` column and `'regex_v1'` value:

```python
async def _insert_atom(
    conn: asyncpg.Connection,
    agent_id: UUID,
    atom: DecomposedAtom,
    embedding: list[float],
    domain_tags: list[str],
    source_type: str = "direct_experience",
    source_ref: UUID | None = None,
    decomposer_version: str = "regex_v1",
) -> asyncpg.Record:
    half_life = HALF_LIVES.get(atom.atom_type, 30.0)
    row = await conn.fetchrow(
        """
        INSERT INTO atoms (
            agent_id, atom_type, text_content, structured, embedding,
            confidence_alpha, confidence_beta,
            source_type, source_ref,
            domain_tags, decay_half_life_days, decay_type, decomposer_version
        ) VALUES ($1,$2,$3,$4,$5::vector,$6,$7,$8,$9,$10,$11,'exponential',$12)
        RETURNING
            id, agent_id, atom_type, text_content, structured,
            confidence_alpha, confidence_beta,
            source_type, domain_tags, created_at, last_accessed, access_count, is_active,
            effective_confidence(
                confidence_alpha, confidence_beta,
                decay_type, decay_half_life_days,
                created_at, last_accessed, access_count
            ) AS confidence_effective
        """,
        agent_id,
        atom.atom_type,
        atom.text,
        json.dumps(atom.structured),
        embedding,
        atom.confidence_alpha,
        atom.confidence_beta,
        source_type,
        source_ref,
        domain_tags,
        half_life,
        decomposer_version,
    )
    return row
```

6. In `store_from_text`, determine the decomposer version and pass it through:

```python
# After the decompose call, determine version
decomposer_version = "haiku_v1" if os.environ.get("ANTHROPIC_API_KEY") else "regex_v1"
```

Then pass `decomposer_version=decomposer_version` to `_insert_atom` calls.

- [ ] **Step 4: Add similarity-based edge creation to `store_from_text`**

The type-based `infer_edges()` won't fire when all atoms are `semantic` (LLM decomposer output). Replace with a similarity-based edge pass that works identically for both decomposers. Add this function to `atom_service.py`:

```python
async def _create_similarity_edges(
    conn: asyncpg.Connection,
    stored_ids: list[UUID],
    embeddings: list[list[float]],
    threshold: float = 0.7,
) -> int:
    """Create 'related' edges between atoms from the same /remember call
    that have cosine similarity above threshold. Preserves graph connectivity
    without depending on type classification."""
    edges_created = 0
    for i in range(len(stored_ids)):
        for j in range(i + 1, len(stored_ids)):
            sim = _cosine_sim(embeddings[i], embeddings[j])
            if sim > threshold:
                try:
                    await conn.execute(
                        """
                        INSERT INTO edges (source_id, target_id, edge_type, weight)
                        VALUES ($1, $2, 'related', $3)
                        ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
                        """,
                        stored_ids[i],
                        stored_ids[j],
                        round(sim, 3),
                    )
                    edges_created += 1
                except Exception:
                    pass
    return edges_created
```

Then in `store_from_text`, replace the `infer_edges` call with the similarity-based edge pass. You'll need to collect embeddings during the atom loop. Change the loop to track embeddings:

```python
    stored_embeddings: list[list[float]] = []
    # ... in the loop, after computing embedding:
    stored_embeddings.append(embedding)
```

Then replace the edge creation block:
```python
    # Create similarity-based edges between atoms in this /remember call
    edges_created = await _create_similarity_edges(conn, stored_ids, stored_embeddings)
```

**Note:** This replaces the old `infer_edges(decomposed)` call entirely. The type-based edge rules (`episodic→semantic`, etc.) are being deprioritised per Task 4, and this similarity-based approach works for both decomposers.

- [ ] **Step 5: Run all tests**

```bash
uv run pytest tests/ -v
```

Expected: All existing tests pass (they don't have ANTHROPIC_API_KEY set, so they fall back to regex). Some edge-related test assertions may need updating if they relied on type-based edge counts.

- [ ] **Step 6: Commit**

```bash
git add mnemo/server/services/atom_service.py tests/test_llm_decomposer.py
git commit -m "feat: wire LLM decomposer into store path with similarity-based edges"
```

---

## Chunk 2: Tasks 2-4 — Retrieval Improvements

### Task 2: Composite Ranking + Post-Retrieval Dedup

**Files:**
- Modify: `mnemo/server/services/atom_service.py` — add dedup, include embedding in SELECT
- Modify: `mnemo/server/services/view_service.py` — composite ranking in recall_shared/recall_all_shared
- Create: `tests/test_recall_ranking.py`

**Important:** Keep the existing composite formula `similarity * (0.7 + 0.3 * effective_confidence)`. The spec says "similarity × confidence" but the current formula is better — pure `similarity * confidence` would cause high-similarity atoms with moderate confidence to rank below lower-similarity atoms with high confidence, which is a regression. The current formula uses confidence as a 0.7-1.0 modifier, which is more appropriate.

- [ ] **Step 1: Write failing test for post-retrieval dedup**

```python
# tests/test_recall_ranking.py
"""Tests for composite ranking and retrieval improvements."""

import pytest


class TestCompositeRanking:
    """Verify that recall ranks by composite score and returns relevance_score."""

    @pytest.mark.asyncio
    async def test_all_results_have_relevance_score(self, client, agent):
        """Every recalled atom should have a non-None relevance_score."""
        aid = agent["id"]
        await client.post(f"/v1/agents/{aid}/remember", json={
            "text": "The PostgreSQL cosine distance operator is <=> and it is confirmed working correctly.",
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "PostgreSQL distance operator",
            "max_results": 10,
            "expand_graph": False,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        assert len(atoms) >= 1
        for atom in atoms:
            assert atom["relevance_score"] is not None
            assert atom["relevance_score"] > 0

    @pytest.mark.asyncio
    async def test_results_sorted_by_relevance_score(self, client, agent):
        """Results should be sorted by relevance_score descending."""
        aid = agent["id"]
        await client.post(f"/v1/agents/{aid}/remember", json={
            "text": "Python is a programming language. PostgreSQL is a database. Redis is a cache.",
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "database systems",
            "max_results": 10,
            "expand_graph": False,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        if len(atoms) >= 2:
            for i in range(len(atoms) - 1):
                assert atoms[i]["relevance_score"] >= atoms[i + 1]["relevance_score"]


class TestPostRetrievalDedup:
    """Verify that near-duplicate atoms are collapsed in results."""

    @pytest.mark.asyncio
    async def test_dedup_collapses_near_identical_atoms(self, client, agent):
        """Two atoms with >0.95 cosine similarity should be collapsed to one."""
        aid = agent["id"]

        # Store two near-identical texts via direct atom creation to bypass decomposer
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "episodic",
            "text_content": "The deployment process requires running database migrations first",
        })
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": "The deployment process requires running database migrations first",
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "deployment database migrations",
            "max_results": 10,
            "expand_graph": False,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]

        # Dedup should collapse these — we should get 1 unique text, not 2
        texts = [a["text_content"] for a in atoms]
        assert texts.count("The deployment process requires running database migrations first") == 1

    @pytest.mark.asyncio
    async def test_dedup_keeps_distinct_atoms(self, client, agent):
        """Atoms with distinct content should not be collapsed."""
        aid = agent["id"]
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": "PostgreSQL uses B-tree indexes by default",
        })
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": "Redis stores data in memory for fast access",
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "database storage",
            "max_results": 10,
            "expand_graph": False,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        assert len(atoms) == 2
```

- [ ] **Step 2: Run tests to verify dedup test fails**

```bash
uv run pytest tests/test_recall_ranking.py -v
```

Expected: `test_dedup_collapses_near_identical_atoms` FAIL (duplicate atoms both returned).

- [ ] **Step 3: Add embedding to retrieval SELECT and add dedup function**

In `mnemo/server/services/atom_service.py`:

1. Add `embedding` to the retrieval query SELECT list (around line 409-418). Add after the `is_active` column:

```sql
            embedding,
```

So the SELECT becomes:
```sql
        SELECT
            id, agent_id, atom_type, text_content, structured,
            confidence_alpha, confidence_beta,
            source_type, domain_tags, created_at, last_accessed, access_count, is_active,
            embedding,
            1 - (embedding <=> $1::vector) AS cosine_sim,
            ...
```

2. Add the dedup function after `_apply_token_budget`:

```python
def _dedup_results(rows: list, threshold: float = 0.95) -> list:
    """Collapse near-duplicate atoms (>threshold cosine similarity).
    Keeps the first occurrence (highest-ranked after sorting).
    Uses embeddings already fetched in the retrieval query."""
    if len(rows) <= 1:
        return rows

    kept = []
    dropped_ids: set = set()

    for i, row in enumerate(rows):
        if row["id"] in dropped_ids:
            continue
        kept.append(row)
        emb_i = row.get("embedding")
        if emb_i is None:
            continue
        for j in range(i + 1, len(rows)):
            if rows[j]["id"] in dropped_ids:
                continue
            emb_j = rows[j].get("embedding")
            if emb_j is None:
                continue
            sim = _cosine_sim(list(emb_i), list(emb_j))
            if sim > threshold:
                dropped_ids.add(rows[j]["id"])

    return kept
```

3. Apply dedup in the `retrieve` function, after the superseded filter and before sorting (after the `_filter_superseded` call):

```python
    rows = _dedup_results(rows)
```

- [ ] **Step 4: Update recall_shared with composite ranking**

In `mnemo/server/services/view_service.py`:

1. In `recall_shared` (around line 344), after filtering by confidence, add composite sort before slicing:

```python
    rows = [r for r in rows if r["confidence_effective"] >= min_confidence]
    rows.sort(
        key=lambda r: r["similarity"] * (0.7 + 0.3 * r["confidence_effective"]),
        reverse=True,
    )
    primary = rows[:max_results]
```

2. Update `_row_to_atom_response` calls in `recall_shared` (around line 384) to pass composite score:

```python
    primary_responses = [
        _row_to_atom_response(r, r["similarity"] * (0.7 + 0.3 * r["confidence_effective"]))
        for r in primary
    ]
```

3. In `recall_all_shared` (around line 469), update the relevance_score:

```python
    atom = _row_to_atom_response(r, relevance_score=r["similarity"] * (0.7 + 0.3 * r["confidence_effective"]))
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_recall_ranking.py tests/test_api.py tests/test_sharing.py -v
```

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add mnemo/server/services/atom_service.py mnemo/server/services/view_service.py tests/test_recall_ranking.py
git commit -m "feat: post-retrieval dedup and composite ranking in shared recall"
```

---

### Task 3: Similarity Floor Check

**Files:**
- Modify: `mnemo/server/models.py`

- [ ] **Step 1: Annotate the min_similarity field**

In `models.py`, the current `RetrieveRequest.min_similarity` at line 71 is `float = 0.2`. Add a description noting the tuning consideration:

```python
    min_similarity: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity floor. Consider raising to 0.3 after LLM decomposer ships and similarity distribution shifts upward.",
    )
```

**Decision:** Leave at 0.2 for now. The spec says "Don't tune this until after Task 1." After observing the new similarity distribution with LLM-decomposed atoms in production, bump to 0.3 if warranted.

- [ ] **Step 2: Commit**

```bash
git add mnemo/server/models.py
git commit -m "docs: annotate min_similarity with post-decomposer tuning note"
```

---

### Task 4: Drop Type from Retrieval and Duplicate Detection

**Files:**
- Modify: `mnemo/server/services/atom_service.py` — remove atom_type filter from retrieval query and `_check_duplicate`
- Modify: `mnemo/server/models.py` — remove `atom_types` from `RetrieveRequest`
- Modify: `mnemo/server/routes/memory.py` — stop passing atom_types
- Modify: existing tests that use `atom_types`

- [ ] **Step 1: Write failing test**

Add to `tests/test_recall_ranking.py`:

```python
class TestNoTypeFiltering:
    """Verify that atom_type is not used in retrieval filtering."""

    @pytest.mark.asyncio
    async def test_recall_returns_all_types(self, client, agent):
        """Recall should return atoms regardless of type."""
        aid = agent["id"]
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "episodic",
            "text_content": "I observed the sky is blue today",
        })
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "procedural",
            "text_content": "Always check the sky color before going outside",
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "sky color",
            "max_results": 10,
            "expand_graph": False,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        assert len(atoms) == 2
        types = {a["atom_type"] for a in atoms}
        assert "episodic" in types
        assert "procedural" in types
```

- [ ] **Step 2: Remove atom_type filter from retrieval query**

In `atom_service.py`, the retrieval query has this WHERE clause (around line 422):

```sql
AND ($3::text[] IS NULL OR atom_type = ANY($3))
```

Remove this line entirely. Renumber the remaining parameters:
- `$3` was `atom_types` → now `$3` is `domain_tags`
- `$4` was `domain_tags` → now `$3`
- `$5` was `over_fetch` → now `$4`

Updated query:
```sql
    rows = await conn.fetch(
        """
        SELECT
            id, agent_id, atom_type, text_content, structured,
            confidence_alpha, confidence_beta,
            source_type, domain_tags, created_at, last_accessed, access_count, is_active,
            embedding,
            1 - (embedding <=> $1::vector) AS cosine_sim,
            effective_confidence(
                confidence_alpha, confidence_beta,
                decay_type, decay_half_life_days,
                created_at, last_accessed, access_count
            ) AS confidence_effective
        FROM atoms
        WHERE agent_id = $2
          AND is_active = true
          AND ($3::text[] IS NULL OR domain_tags && $3)
        ORDER BY cosine_sim DESC
        LIMIT $4
        """,
        embedding,
        agent_id,
        domain_tags,
        over_fetch,
    )
```

- [ ] **Step 3: Remove atom_types from retrieve signature**

Update the `retrieve` function signature to remove `atom_types`:

```python
async def retrieve(
    conn: asyncpg.Connection,
    agent_id: UUID,
    query: str,
    domain_tags: list[str] | None,
    min_confidence: float,
    min_similarity: float,
    max_results: int,
    expand_graph: bool,
    expansion_depth: int,
    include_superseded: bool,
    similarity_drop_threshold: float | None = 0.3,
    verbosity: str = "full",
    max_content_chars: int = 200,
    max_total_tokens: int | None = None,
) -> dict:
```

- [ ] **Step 4: Remove atom_type from `_check_duplicate`**

Change `_check_duplicate` to not filter by atom_type. Remove the `atom_type` parameter and the `AND atom_type = $3` clause:

```python
async def _check_duplicate(
    conn: asyncpg.Connection,
    agent_id: UUID,
    embedding: list[float],
) -> asyncpg.Record | None:
    """Return the most similar existing active atom if similarity > threshold."""
    threshold = settings.duplicate_similarity_threshold
    row = await conn.fetchrow(
        """
        SELECT id, confidence_alpha, confidence_beta,
               1 - (embedding <=> $1::vector) AS similarity
        FROM atoms
        WHERE agent_id = $2
          AND is_active = true
          AND 1 - (embedding <=> $1::vector) > $3
        ORDER BY similarity DESC
        LIMIT 1
        """,
        embedding,
        agent_id,
        threshold,
    )
    return row
```

- [ ] **Step 5: Update all callers of `_check_duplicate`**

Two callers need updating:

1. In `store_from_text` (around line 274):
```python
# Before:
duplicate = await _check_duplicate(conn, agent_id, atom.atom_type, embedding)
# After:
duplicate = await _check_duplicate(conn, agent_id, embedding)
```

2. In `store_explicit` (around line 351):
```python
# Before:
duplicate = await _check_duplicate(conn, agent_id, atom_type, embedding)
# After:
duplicate = await _check_duplicate(conn, agent_id, embedding)
```

- [ ] **Step 6: Remove `atom_types` from RetrieveRequest model**

In `mnemo/server/models.py`, remove line 68:
```python
    atom_types: Optional[list[str]] = None
```

- [ ] **Step 7: Update the recall route**

In `mnemo/server/routes/memory.py`, remove `atom_types=body.atom_types` from the `retrieve()` call (around line 53):

```python
        result = await atom_service.retrieve(
            conn=conn,
            agent_id=agent_uuid,
            query=body.query,
            domain_tags=body.domain_tags,
            min_confidence=body.min_confidence,
            min_similarity=body.min_similarity,
            max_results=body.max_results,
            expand_graph=body.expand_graph,
            expansion_depth=body.expansion_depth,
            include_superseded=body.include_superseded,
            similarity_drop_threshold=body.similarity_drop_threshold,
            verbosity=body.verbosity,
            max_content_chars=body.max_content_chars,
            max_total_tokens=body.max_total_tokens,
        )
```

- [ ] **Step 8: Fix existing tests that use atom_types**

Search for `atom_types` in test files and remove them from recall requests:

```bash
grep -rn "atom_types" tests/
```

Update any test that passes `atom_types` to recall — remove the parameter. If a test specifically tests type filtering, either remove the test or update it to test that the parameter is no longer accepted.

- [ ] **Step 9: Run all tests**

```bash
uv run pytest tests/ -v
```

Expected: All pass.

- [ ] **Step 10: Commit**

```bash
git add mnemo/server/services/atom_service.py mnemo/server/models.py mnemo/server/routes/memory.py tests/
git commit -m "feat: remove atom_type from retrieval filtering and duplicate detection"
```

---

## Chunk 3: Validation

### Task 5: Final Integration Verification

- [ ] **Step 1: Run the full test suite**

```bash
uv run pytest tests/ -v --tb=short
```

All tests must pass.

- [ ] **Step 2: Manual smoke test**

```bash
# Start the dev server (with API key for LLM decomposer)
ANTHROPIC_API_KEY=<key> uv run uvicorn mnemo.server.main:app --reload
```

In another terminal, test the full flow:
```bash
# Store a memory
curl -s -X POST http://localhost:8000/v1/agents/<agent_id>/remember \
  -H "Content-Type: application/json" \
  -d '{"text": "Mnemo uses Beta distributions for confidence scoring. Each atom has alpha and beta parameters."}' | python -m json.tool

# Recall
curl -s -X POST http://localhost:8000/v1/agents/<agent_id>/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "How does confidence work?", "max_results": 5}' | python -m json.tool
```

Verify:
- Atoms have `relevance_score` in response (composite)
- No duplicate atoms in results
- Results are ranked by relevance_score descending
- Similarity scores are reasonable (hopefully higher with LLM-decomposed atoms)

- [ ] **Step 3: Commit any fixes**

```bash
git add -A
git commit -m "fix: integration fixes from smoke testing"
```

---

## Summary of Changes

| Task | What Changes | Key Files |
|------|-------------|-----------|
| 1.1 | Comparison script (throwaway) | `decomposer_comparison.py` |
| 1.2 | Schema additions | `schema.sql`, `tests/conftest.py` |
| 1.3 | LLM decomposer module | `llm_decomposer.py`, `tests/test_llm_decomposer.py` |
| 1.4 | Wire into store path | `atom_service.py` |
| 2 | Post-retrieval dedup + shared ranking | `atom_service.py`, `view_service.py`, `tests/test_recall_ranking.py` |
| 3 | Similarity floor annotation | `models.py` |
| 4 | Remove type from retrieval + dedup | `atom_service.py`, `models.py`, `memory.py`, tests |

## Design Decisions

1. **Kept existing composite formula** `similarity * (0.7 + 0.3 * confidence)` instead of the spec's `similarity * confidence`. Pure product would rank moderate-confidence atoms too low relative to their actual relevance.

2. **No async store worker.** The spec's async architecture is a latency optimization, not a quality improvement. LLM decomposer latency is acceptable on the synchronous store path. Can add async later if needed.

3. **Similarity-based edges replace type-based edges.** `infer_edges()` (type-based: episodic→semantic, etc.) is replaced by a pairwise cosine similarity pass that creates `"related"` edges between atoms with >0.7 similarity from the same `/remember` call. This works identically for both decomposers and doesn't depend on classification accuracy.

4. **Dedup uses embeddings from the retrieval query** (added to SELECT) instead of a separate DB round-trip. O(n²) comparison on `max_results*2` atoms (typically ≤20) — negligible cost.

5. **Fallback to regex decomposer** when `ANTHROPIC_API_KEY` is not set. This keeps tests working without API calls and allows running without external dependencies.
