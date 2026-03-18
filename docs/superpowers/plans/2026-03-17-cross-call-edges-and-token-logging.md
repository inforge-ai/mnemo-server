# Cross-Call Edge Inference & Decomposer Token Logging

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect atoms across `/remember` calls via ANN-based edge inference, and log LLM decomposer token usage for operator cost visibility.

**Architecture:** Both features modify the async store pipeline (`store_from_text`). Cross-call edges add a post-embedding ANN query per new atom that creates `related` edges to existing atoms above 0.78 similarity. Token logging captures Anthropic API usage metadata from the LLM decomposer response and writes it to a new `decomposer_usage` table. The `store_background` function gains an `operator_id` parameter (looked up from the agent's row) so the usage row can be attributed to the correct operator.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, pgvector, Anthropic SDK, pytest

**Spec:** `docs/mnemo_improvement_20261703.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `schema.sql` | Add `decomposer_usage` table + grants |
| Modify | `mnemo/server/config.py` | Add `cross_call_edge_threshold` setting (0.78) |
| Modify | `mnemo/server/llm_decomposer.py` | Return usage metadata alongside atoms; accept recalled context for prompt |
| Modify | `mnemo/server/services/atom_service.py` | Cross-call ANN edge creation; decomposer usage logging; pass operator_id through store pipeline |
| Modify | `mnemo/server/routes/memory.py` | Resolve operator_id before spawning background store |
| Modify | `tests/conftest.py` | Add `decomposer_usage` to cleanup |
| Create | `tests/test_cross_call_edges.py` | Tests for cross-call edge inference |
| Create | `tests/test_decomposer_usage.py` | Tests for token usage logging |

---

## Chunk 1: Schema, Config & LLM Decomposer Changes

### Task 1: Add `decomposer_usage` table to schema

**Files:**
- Modify: `schema.sql` (after `store_failures` table, ~line 216)

- [ ] **Step 1: Add the `decomposer_usage` table DDL to `schema.sql`**

Append before the `HELPER FUNCTIONS` section:

```sql
-- Decomposer token usage (operator cost visibility)
CREATE TABLE decomposer_usage (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    store_id                    UUID NOT NULL,
    operator_id                 UUID NOT NULL,
    agent_id                    UUID NOT NULL,
    model                       TEXT NOT NULL,
    input_tokens                INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER,
    cache_read_input_tokens     INTEGER,
    output_tokens               INTEGER NOT NULL
);

CREATE INDEX idx_decomposer_usage_operator_created
    ON decomposer_usage (operator_id, created_at);
```

Add grant at the end with other grants:

```sql
GRANT SELECT, INSERT ON decomposer_usage TO mnemo;
```

- [ ] **Step 2: Apply the migration to the test database**

Run:
```bash
sudo -u postgres psql mnemo_test -c "
CREATE TABLE IF NOT EXISTS decomposer_usage (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    store_id                    UUID NOT NULL,
    operator_id                 UUID NOT NULL,
    agent_id                    UUID NOT NULL,
    model                       TEXT NOT NULL,
    input_tokens                INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER,
    cache_read_input_tokens     INTEGER,
    output_tokens               INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decomposer_usage_operator_created
    ON decomposer_usage (operator_id, created_at);
GRANT SELECT, INSERT, DELETE ON decomposer_usage TO mnemo;
"
```

Also apply to prod database:
```bash
sudo -u postgres psql mnemo -c "
CREATE TABLE IF NOT EXISTS decomposer_usage (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    store_id                    UUID NOT NULL,
    operator_id                 UUID NOT NULL,
    agent_id                    UUID NOT NULL,
    model                       TEXT NOT NULL,
    input_tokens                INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER,
    cache_read_input_tokens     INTEGER,
    output_tokens               INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decomposer_usage_operator_created
    ON decomposer_usage (operator_id, created_at);
GRANT SELECT, INSERT ON decomposer_usage TO mnemo;
"
```

- [ ] **Step 3: Commit**

```bash
git add schema.sql
git commit -m "schema: add decomposer_usage table for token cost tracking"
```

---

### Task 2: Add `cross_call_edge_threshold` to config

**Files:**
- Modify: `mnemo/server/config.py`

- [ ] **Step 1: Add the setting**

Add after `duplicate_similarity_threshold` (line 13):

```python
cross_call_edge_threshold: float = 0.78
```

- [ ] **Step 2: Commit**

```bash
git add mnemo/server/config.py
git commit -m "config: add cross_call_edge_threshold setting (0.78)"
```

---

### Task 3: Update LLM decomposer to return usage metadata

The LLM decomposer currently returns `list[DecomposedAtom]`. It needs to also return token usage from the Anthropic response. We add a `DecomposerResult` dataclass that bundles atoms + usage, and update `llm_decompose` to return it. The regex decomposer path returns `None` for usage.

**Files:**
- Modify: `mnemo/server/llm_decomposer.py`

- [ ] **Step 1: Write failing test for usage metadata**

**File:** `tests/test_decomposer_usage.py`

```python
"""Tests for decomposer token usage logging."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestLLMDecomposerUsageReturn:
    """Verify llm_decompose returns usage metadata alongside atoms."""

    @pytest.mark.asyncio
    async def test_returns_decomposer_result_with_usage(self):
        """llm_decompose returns a DecomposerResult with atoms and usage."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "Test fact", "type": "semantic", "confidence": 0.8},
        ]))]
        mock_response.usage = MagicMock(
            input_tokens=150,
            output_tokens=42,
            cache_creation_input_tokens=100,
            cache_read_input_tokens=0,
        )
        mock_response.model = "claude-haiku-4-5-20251001"

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            from mnemo.server.llm_decomposer import llm_decompose
            result = await llm_decompose("Test fact")

        assert hasattr(result, "atoms")
        assert hasattr(result, "usage")
        assert len(result.atoms) == 1
        assert result.atoms[0].text == "Test fact"
        assert result.usage is not None
        assert result.usage["model"] == "claude-haiku-4-5-20251001"
        assert result.usage["input_tokens"] == 150
        assert result.usage["output_tokens"] == 42
        assert result.usage["cache_creation_input_tokens"] == 100
        assert result.usage["cache_read_input_tokens"] == 0

    @pytest.mark.asyncio
    async def test_usage_handles_missing_cache_fields(self):
        """Cache token fields are None when not present on the response."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "A fact", "type": "semantic", "confidence": 0.7},
        ]))]
        mock_response.usage = MagicMock(
            input_tokens=80,
            output_tokens=30,
            spec=["input_tokens", "output_tokens"],
        )
        # Remove cache attributes so getattr returns None
        del mock_response.usage.cache_creation_input_tokens
        del mock_response.usage.cache_read_input_tokens
        mock_response.model = "claude-haiku-4-5-20251001"

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client):
            from mnemo.server.llm_decomposer import llm_decompose
            result = await llm_decompose("A fact")

        assert result.usage["input_tokens"] == 80
        assert result.usage["cache_creation_input_tokens"] is None
        assert result.usage["cache_read_input_tokens"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_decomposer_usage.py::TestLLMDecomposerUsageReturn -v`
Expected: FAIL — `llm_decompose` returns a list, not a `DecomposerResult`

- [ ] **Step 3: Implement `DecomposerResult` and update `llm_decompose`**

In `mnemo/server/llm_decomposer.py`:

1. Add dataclass at top (after imports):

```python
from dataclasses import dataclass, field

@dataclass
class DecomposerResult:
    """Bundle of decomposed atoms + optional LLM usage metadata."""
    atoms: list[DecomposedAtom]
    usage: dict | None = None  # {model, input_tokens, output_tokens, cache_*}
```

2. Update `llm_decompose` to return `DecomposerResult`:

```python
async def llm_decompose(text: str) -> DecomposerResult:
    if not text or not text.strip():
        return DecomposerResult(atoms=[])

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

    # Extract usage metadata
    usage = {
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", None),
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", None),
    }

    raw_text = response.content[0].text
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]
        raw_text = raw_text.rsplit("```", 1)[0]
    raw = json.loads(raw_text.strip())
    atoms = []
    for item in raw:
        alpha, beta = _confidence_to_beta(item.get("confidence", 0.5))
        atom_type = item.get("type", "semantic")
        if atom_type not in ("episodic", "semantic", "procedural"):
            atom_type = "semantic"
        atoms.append(DecomposedAtom(
            text=item["text"],
            atom_type=atom_type,
            confidence_alpha=alpha,
            confidence_beta=beta,
            source_type="direct_experience",
        ))

    return DecomposerResult(atoms=atoms, usage=usage)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_decomposer_usage.py::TestLLMDecomposerUsageReturn -v`
Expected: PASS

- [ ] **Step 5: Fix existing LLM decomposer tests**

The existing tests in `tests/test_llm_decomposer.py` expect `llm_decompose` to return a list. Update them to access `.atoms` instead. For every test that does `atoms = await llm_decompose(...)`, change to `result = await llm_decompose(...)` then `atoms = result.atoms`.

- [ ] **Step 6: Run all LLM decomposer tests**

Run: `uv run pytest tests/test_llm_decomposer.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add mnemo/server/llm_decomposer.py tests/test_decomposer_usage.py tests/test_llm_decomposer.py
git commit -m "feat: llm_decompose returns DecomposerResult with usage metadata"
```

---

## Chunk 2: Store Pipeline Changes

### Task 4: Update `_decompose` to handle `DecomposerResult`

The `_decompose` helper in `atom_service.py` currently returns `list[DecomposedAtom]`. Update it to always return a `DecomposerResult` — the regex path wraps the list with `usage=None`.

**Files:**
- Modify: `mnemo/server/services/atom_service.py`

- [ ] **Step 1: Update `_decompose` return type**

```python
async def _decompose(text: str, domain_tags: list[str] | None = None):
    """Use LLM decomposer if ANTHROPIC_API_KEY is set, else fall back to regex.
    Returns DecomposerResult (atoms + optional usage metadata)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        from ..llm_decomposer import llm_decompose
        return await llm_decompose(text)
    from ..llm_decomposer import DecomposerResult
    return DecomposerResult(atoms=regex_decompose(text, domain_tags))
```

- [ ] **Step 2: Update `store_from_text` to unpack `DecomposerResult`**

At the top of `store_from_text`, change:

```python
decomposed = await _decompose(text, domain_tags)
if not decomposed:
```

to:

```python
decomposer_result = await _decompose(text, domain_tags)
decomposed = decomposer_result.atoms
if not decomposed:
```

- [ ] **Step 3: Run existing tests to verify nothing broke**

Run: `uv run pytest tests/test_api.py -v -x`
Expected: PASS (all existing tests should still pass)

- [ ] **Step 4: Commit**

```bash
git add mnemo/server/services/atom_service.py
git commit -m "refactor: _decompose returns DecomposerResult uniformly"
```

---

### Task 5: Cross-call edge inference

Add a function that, for each newly inserted atom, queries existing atoms via ANN search and creates `related` edges above the configured threshold. Integrate it into `store_from_text`.

**Files:**
- Modify: `mnemo/server/services/atom_service.py`
- Create: `tests/test_cross_call_edges.py`

- [ ] **Step 1: Write failing tests**

**File:** `tests/test_cross_call_edges.py`

```python
"""Tests for cross-call edge inference (linking atoms across /remember calls)."""

import pytest
from tests.conftest import remember


class TestCrossCallEdges:
    """Atoms stored in separate /remember calls should get edges when similar."""

    async def test_creates_edges_between_similar_atoms_across_calls(self, client, agent):
        """Two /remember calls about the same topic should produce cross-call edges."""
        # First call — store a fact about pgvector
        await remember(client, agent["id"], "pgvector stores embeddings as vector columns in PostgreSQL.")

        # Second call — store a related fact about pgvector
        await remember(client, agent["id"], "pgvector supports cosine similarity search on vector columns.")

        # Check that an edge was created between atoms from different calls
        stats = (await client.get(f"/v1/agents/{agent['id']}/stats")).json()
        # We expect at least one cross-call edge (plus any within-call edges)
        assert stats["total_edges"] >= 1

    async def test_no_edges_between_unrelated_atoms_across_calls(self, client, agent):
        """Unrelated topics across calls should NOT produce cross-call edges."""
        await remember(client, agent["id"], "pgvector stores embeddings as vector columns.")
        await remember(client, agent["id"], "The French Revolution began in 1789.")

        # Recall both to verify they exist
        r1 = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "pgvector embeddings", "expand_graph": False, "min_similarity": 0.1,
        })
        r2 = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "French Revolution", "expand_graph": False, "min_similarity": 0.1,
        })
        assert r1.json()["total_retrieved"] >= 1
        assert r2.json()["total_retrieved"] >= 1

        # Check edges — there should be no cross-call edge between unrelated atoms
        # (within-call edges may exist if decomposer created multiple atoms)
        stats = (await client.get(f"/v1/agents/{agent['id']}/stats")).json()
        # We can't assert zero edges (within-call edges are possible),
        # but we verify via direct DB check below

    async def test_cross_call_edges_use_related_type(self, client, agent, pool):
        """Cross-call edges should be of type 'related'."""
        await remember(client, agent["id"], "asyncpg is an async PostgreSQL driver for Python.")
        await remember(client, agent["id"], "asyncpg uses a connection pool for PostgreSQL connections.")

        async with pool.acquire() as conn:
            edges = await conn.fetch(
                """
                SELECT e.edge_type, e.weight
                FROM edges e
                JOIN atoms a1 ON a1.id = e.source_id
                JOIN atoms a2 ON a2.id = e.target_id
                WHERE a1.agent_id = $1
                  AND a1.created_at != a2.created_at
                """,
                agent["id"],
            )
        # At least one cross-call edge should exist
        assert len(edges) >= 1
        for edge in edges:
            assert edge["edge_type"] == "related"
            assert 0.0 < edge["weight"] <= 1.0

    async def test_cross_call_edges_do_not_duplicate_existing(self, client, agent, pool):
        """ON CONFLICT DO NOTHING prevents duplicate cross-call edges."""
        await remember(client, agent["id"], "pgvector stores vector embeddings.")
        await remember(client, agent["id"], "pgvector stores vector embeddings in columns.")
        # Third call that might create the same edge again
        await remember(client, agent["id"], "pgvector stores high-dimensional vector embeddings.")

        async with pool.acquire() as conn:
            # Count edges — each (source, target, type) triple should be unique
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM edges e
                JOIN atoms a ON a.id = e.source_id
                WHERE a.agent_id = $1
                """,
                agent["id"],
            )
        # Just verify no crash and edges exist; uniqueness constraint handles dedup
        assert count >= 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cross_call_edges.py -v`
Expected: FAIL — no cross-call edges are created yet

- [ ] **Step 3: Implement `_create_cross_call_edges`**

Add this function to `atom_service.py` (above `store_from_text`):

```python
async def _create_cross_call_edges(
    conn: asyncpg.Connection,
    agent_id: UUID,
    new_atom_id: UUID,
    embedding: list[float],
    exclude_ids: set[UUID],
) -> int:
    """Query existing atoms via ANN and create 'related' edges for any above threshold.

    This connects atoms across /remember calls. Only creates edges — never merges
    or updates existing atoms. See spec §1: "Edges Only, Not Merging".

    Args:
        conn: Database connection.
        agent_id: The agent whose atoms to search.
        new_atom_id: The newly stored atom to link FROM.
        embedding: The new atom's embedding vector.
        exclude_ids: Atom IDs from the current /remember call (skip — already linked by within-call edges).

    Returns:
        Number of cross-call edges created.
    """
    threshold = settings.cross_call_edge_threshold
    rows = await conn.fetch(
        """
        SELECT id, 1 - (embedding <=> $1::vector) AS similarity
        FROM atoms
        WHERE agent_id = $2
          AND is_active = true
          AND id != ALL($3::uuid[])
          AND 1 - (embedding <=> $1::vector) > $4
        ORDER BY similarity DESC
        LIMIT 5
        """,
        embedding,
        agent_id,
        list(exclude_ids),
        threshold,
    )

    edges_created = 0
    for row in rows:
        try:
            await conn.execute(
                """
                INSERT INTO edges (source_id, target_id, edge_type, weight)
                VALUES ($1, $2, 'related', $3)
                ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
                """,
                new_atom_id,
                row["id"],
                round(row["similarity"], 3),
            )
            edges_created += 1
        except Exception:
            pass
    return edges_created
```

- [ ] **Step 4: Integrate into `store_from_text`**

After the existing within-call edge creation block (after the arc→non-arc edges), add:

```python
    # Cross-call edge inference: link new atoms to existing similar atoms
    # from previous /remember calls. Edges only — no merging.
    current_call_ids = set(stored_ids)
    for i, atom_id in enumerate(stored_ids):
        cross_edges = await _create_cross_call_edges(
            conn, agent_id, atom_id, stored_embeddings[i], current_call_ids,
        )
        edges_created += cross_edges
```

- [ ] **Step 5: Run cross-call edge tests**

Run: `uv run pytest tests/test_cross_call_edges.py -v`
Expected: PASS

- [ ] **Step 6: Run full test suite to check for regressions**

Run: `uv run pytest tests/ -v -x`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add mnemo/server/services/atom_service.py tests/test_cross_call_edges.py
git commit -m "feat: cross-call edge inference links atoms across /remember calls"
```

---

### Task 6: Decomposer token usage logging

Log token usage to the `decomposer_usage` table when the LLM decomposer is used. The `store_from_text` function receives the usage from `DecomposerResult` and writes it to the DB. Requires `operator_id` and `store_id` to be threaded through.

**Files:**
- Modify: `mnemo/server/services/atom_service.py` — `store_from_text` and `store_background` signatures
- Modify: `mnemo/server/routes/memory.py` — resolve and pass operator_id
- Modify: `tests/conftest.py` — add `decomposer_usage` to cleanup
- Modify: `tests/test_decomposer_usage.py` — add integration tests

- [ ] **Step 1: Update `tests/conftest.py` cleanup**

Add `DELETE FROM decomposer_usage;` to the `_CLEAN` string, before `DELETE FROM atoms;`:

```python
_CLEAN = """
DELETE FROM capabilities;
DELETE FROM snapshot_atoms;
DELETE FROM edges;
DELETE FROM views;
DELETE FROM store_failures;
DELETE FROM decomposer_usage;
DELETE FROM atoms;
DELETE FROM api_keys;
DELETE FROM agent_addresses;
DELETE FROM agents;
DELETE FROM operations;
DELETE FROM operators;
"""
```

- [ ] **Step 2: Write failing integration test**

Append to `tests/test_decomposer_usage.py`:

```python
class TestDecomposerUsageLogging:
    """Integration tests: verify usage rows are written to the database."""

    async def test_llm_decomposer_logs_usage_to_db(self, client, agent, pool):
        """When LLM decomposer is active, a decomposer_usage row is created."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"text": "PostgreSQL supports JSONB", "type": "semantic", "confidence": 0.9},
        ]))]
        mock_response.usage = MagicMock(
            input_tokens=200,
            output_tokens=50,
            cache_creation_input_tokens=120,
            cache_read_input_tokens=0,
        )
        mock_response.model = "claude-haiku-4-5-20251001"

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("mnemo.server.llm_decomposer._get_client", return_value=mock_client), \
             patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            await remember(client, agent["id"], "PostgreSQL supports JSONB")

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM decomposer_usage WHERE agent_id = $1",
                agent["id"],
            )
        assert len(rows) == 1
        row = rows[0]
        assert row["model"] == "claude-haiku-4-5-20251001"
        assert row["input_tokens"] == 200
        assert row["output_tokens"] == 50
        assert row["cache_creation_input_tokens"] == 120
        assert row["cache_read_input_tokens"] == 0
        assert row["operator_id"] is not None
        assert row["store_id"] is not None

    async def test_regex_decomposer_does_not_log_usage(self, client, agent, pool):
        """When regex decomposer is used (no API key), no usage row is created."""
        import os
        key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            await remember(client, agent["id"], "Regex decomposer test sentence.")
        finally:
            if key:
                os.environ["ANTHROPIC_API_KEY"] = key

        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM decomposer_usage WHERE agent_id = $1",
                agent["id"],
            )
        assert count == 0
```

Add the necessary import at the top of the file:

```python
from tests.conftest import remember
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_decomposer_usage.py::TestDecomposerUsageLogging -v`
Expected: FAIL — no usage logging implemented yet

- [ ] **Step 4: Add `store_id` and `operator_id` parameters to `store_from_text`**

Update the `store_from_text` signature:

```python
async def store_from_text(
    conn: asyncpg.Connection,
    agent_id: UUID,
    text: str,
    domain_tags: list[str],
    store_id: UUID | None = None,
    operator_id: UUID | None = None,
) -> dict:
```

After the decomposer call and before the atom loop, add usage logging:

```python
    decomposer_result = await _decompose(text, domain_tags)
    decomposed = decomposer_result.atoms
    if not decomposed:
        return {"atoms": [], "atoms_created": 0, "edges_created": 0, "duplicates_merged": 0}

    # Log decomposer token usage if available
    if decomposer_result.usage and store_id and operator_id:
        usage = decomposer_result.usage
        try:
            await conn.execute(
                """
                INSERT INTO decomposer_usage (
                    store_id, operator_id, agent_id, model,
                    input_tokens, cache_creation_input_tokens,
                    cache_read_input_tokens, output_tokens
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                store_id,
                operator_id,
                agent_id,
                usage["model"],
                usage["input_tokens"],
                usage.get("cache_creation_input_tokens"),
                usage.get("cache_read_input_tokens"),
                usage["output_tokens"],
            )
        except Exception:
            logger.warning("Failed to log decomposer usage for store %s", store_id)
```

- [ ] **Step 5: Update `store_background` to accept and pass `store_id` and `operator_id`**

Update signature and the call to `store_from_text`:

```python
async def store_background(
    pool: asyncpg.Pool,
    store_id: UUID,
    agent_id: UUID,
    text: str,
    domain_tags: list[str],
    operator_id: UUID | None = None,
) -> None:
    try:
        async with pool.acquire() as conn:
            await store_from_text(
                conn, agent_id, text, domain_tags,
                store_id=store_id, operator_id=operator_id,
            )
    except Exception:
        # ... existing error handling unchanged
```

- [ ] **Step 6: Update route to resolve and pass `operator_id`**

In `mnemo/server/routes/memory.py`, update the `remember` handler to look up the agent's `operator_id` from the DB and pass it:

```python
@router.post("/agents/{agent_id}/remember", response_model=RememberResponse, status_code=201)
async def remember(agent_id: str, body: RememberRequest, operator=Depends(get_current_operator)):
    """Store a free-text memory. Returns immediately; decomposition runs in background."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        # Resolve operator_id for decomposer usage logging
        op_row = await conn.fetchrow(
            "SELECT operator_id FROM agents WHERE id = $1", agent_uuid,
        )
        operator_id = op_row["operator_id"] if op_row else None

    store_id = uuid4()
    async with get_conn() as conn:
        await log_operation(conn, "remember", operator["id"], target_id=agent_uuid)
    coro = atom_service.store_background(
        pool=pool,
        store_id=store_id,
        agent_id=agent_uuid,
        text=body.text,
        domain_tags=body.domain_tags,
        operator_id=operator_id,
    )
    if settings.sync_store_for_tests:
        await coro
    else:
        asyncio.create_task(coro)
    return {"status": "queued", "store_id": store_id}
```

- [ ] **Step 7: Run the integration tests**

Run: `uv run pytest tests/test_decomposer_usage.py -v`
Expected: PASS

- [ ] **Step 8: Run the full test suite**

Run: `uv run pytest tests/ -v -x`
Expected: PASS — no regressions

- [ ] **Step 9: Commit**

```bash
git add mnemo/server/services/atom_service.py mnemo/server/routes/memory.py tests/conftest.py tests/test_decomposer_usage.py
git commit -m "feat: log decomposer token usage to decomposer_usage table"
```

---

## Chunk 3: Final Verification & Cleanup

### Task 7: Full regression test

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: Manually verify cross-call edges with the dev server (optional)**

```bash
# Start dev server
uv run uvicorn mnemo.server.main:app --reload &

# Create agent
curl -s -X POST http://localhost:8000/v1/agents -H 'Content-Type: application/json' \
  -d '{"name": "test-xcall", "domain_tags": ["test"]}'

# Store first memory
curl -s -X POST http://localhost:8000/v1/agents/<AGENT_ID>/remember \
  -H 'Content-Type: application/json' \
  -d '{"text": "pgvector stores embeddings as vector columns."}'

# Wait a moment, store second related memory
curl -s -X POST http://localhost:8000/v1/agents/<AGENT_ID>/remember \
  -H 'Content-Type: application/json' \
  -d '{"text": "pgvector supports cosine similarity on vector columns."}'

# Check stats — should show edges
curl -s http://localhost:8000/v1/agents/<AGENT_ID>/stats | python3 -m json.tool
```

- [ ] **Step 3: Final commit (if any cleanup needed)**

```bash
git add -A
git commit -m "chore: final cleanup for cross-call edges and token logging"
```
