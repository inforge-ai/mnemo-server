# Chassis Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 6 targeted improvements to the Mnemo server based on design partner feedback — surfacing confidence metadata, verifying Bayesian updates, penalising super-atoms, enriching stats, adding store status tracking, and tightening input validation.

**Architecture:** All changes are to the REST API server (`mnemo-server`). No MCP tool changes (MCP is in a separate repo `../mnemo-client`). Changes touch the recall pipeline, remember pipeline, stats endpoint, and add one new REST endpoint. One new DB table (`store_jobs`), no new columns needed (`access_count` and `source_type` already exist).

**Tech Stack:** Python 3.12, FastAPI, asyncpg, PostgreSQL 16 + pgvector, pydantic-settings, pytest. Package manager: `uv`.

**Spec:** `docs/mnemo_chassis_spec.md`

**Branch:** `feat/chassis-improvements` (already created)

---

## Test Conventions

All tests must follow these conventions from `tests/conftest.py`:

- **Fixtures:** Use `agent` (returns full agent JSON dict — extract ID via `agent["id"]`), `pool` (asyncpg pool), `client` (httpx AsyncClient)
- **URL prefix:** All routes are mounted at `/v1`. Use `/v1/agents/{id}/remember`, `/v1/agents/{id}/recall`, `/v1/agents/{id}/stats`, etc.
- **Marker:** All async test files need `pytestmark = pytest.mark.anyio`
- **Sync store:** Tests run with `MNEMO_SYNC_STORE_FOR_TESTS=true`, so `/remember` blocks until atoms are stored

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `mnemo/server/services/atom_service.py` | Modify | Tasks 1, 2, 3, 4: confidence metadata in `_row_to_atom_response`, Bayesian update docs, specificity penalty via `composite_score()`, stats enrichment in `get_agent_stats` |
| `mnemo/server/services/view_service.py` | Modify | Task 3: use shared `composite_score()` in shared view recall |
| `mnemo/server/models.py` | Modify | Tasks 1, 4, 5, 6: new fields on `AtomResponse`, `AgentStats`; new `StoreJobResponse` model |
| `mnemo/server/routes/memory.py` | Modify | Tasks 5, 6: input validation in `remember()`, store job tracking, new status endpoint |
| `schema.sql` | Modify | Task 5: `store_jobs` table DDL |
| `tests/conftest.py` | Modify | Task 5: add `DELETE FROM store_jobs;` to `_CLEAN` |
| `tests/test_input_validation.py` | Create | Task 6 |
| `tests/test_bayesian_update.py` | Create | Task 2 |
| `tests/test_confidence_metadata.py` | Create | Task 1 |
| `tests/test_specificity_penalty.py` | Create | Task 3 |
| `tests/test_stats_enrichment.py` | Create | Task 4 |
| `tests/test_store_status.py` | Create | Task 5 |

---

## Parallelisation

Tasks 6, 2, 3, and 5 are fully independent. Task 1 depends on Task 2 (need confirmed Bayesian updates before surfacing them). Task 4 is independent (access_count already exists).

**Wave 1 (parallel):** Tasks 6, 2, 3, 5
**Wave 2 (parallel):** Tasks 1, 4

---

## Task 1: Surface Confidence Metadata in Recall Output

**Depends on:** Task 2 (Bayesian update verified)

**Files:**
- Modify: `mnemo/server/services/atom_service.py:276-294` (`_row_to_atom_response`)
- Modify: `mnemo/server/services/atom_service.py:253-265` (`_apply_verbosity`)
- Modify: `mnemo/server/models.py:47-61` (`AtomResponse`)
- Test: `tests/test_confidence_metadata.py`

### Steps

- [ ] **Step 1: Write the failing test**

```python
# tests/test_confidence_metadata.py
import pytest

pytestmark = pytest.mark.anyio


async def test_recall_full_verbosity_includes_confidence_metadata(client, agent):
    """At verbosity=full, recall output should include alpha, beta."""
    agent_id = agent["id"]
    await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "The project deadline is June 15."},
    )

    resp = await client.post(
        f"/v1/agents/{agent_id}/recall",
        json={"query": "project deadline", "verbosity": "full"},
    )
    assert resp.status_code == 200
    atoms = resp.json()["atoms"]
    assert len(atoms) >= 1
    atom = atoms[0]
    assert "confidence_alpha" in atom
    assert "confidence_beta" in atom
    assert atom["confidence_alpha"] > 0
    assert atom["confidence_beta"] > 0


async def test_recall_summary_verbosity_excludes_confidence_metadata(client, agent):
    """At verbosity=summary, alpha/beta should NOT be in the response."""
    agent_id = agent["id"]
    await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "The project deadline is June 15."},
    )

    resp = await client.post(
        f"/v1/agents/{agent_id}/recall",
        json={"query": "project deadline", "verbosity": "summary"},
    )
    assert resp.status_code == 200
    atoms = resp.json()["atoms"]
    assert len(atoms) >= 1
    atom = atoms[0]
    assert "confidence_alpha" not in atom
    assert "confidence_beta" not in atom
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_confidence_metadata.py -v`
Expected: FAIL — `confidence_alpha` not in atom response

- [ ] **Step 3: Add optional fields to AtomResponse model**

In `mnemo/server/models.py`, add optional fields to `AtomResponse`:

```python
class AtomResponse(BaseModel):
    id: UUID
    agent_id: UUID
    atom_type: str
    text_content: str
    structured: dict
    confidence_expected: float
    confidence_effective: float
    relevance_score: Optional[float] = None
    source_type: str
    domain_tags: list[str]
    created_at: datetime
    last_accessed: Optional[datetime]
    access_count: int
    is_active: bool
    # Confidence metadata — populated at verbosity=full only
    confidence_alpha: Optional[float] = None
    confidence_beta: Optional[float] = None
```

- [ ] **Step 4: Include alpha/beta in `_row_to_atom_response`**

In `mnemo/server/services/atom_service.py`, modify `_row_to_atom_response` (line 276) to include alpha/beta:

```python
def _row_to_atom_response(row: asyncpg.Record, relevance_score: float | None = None) -> dict:
    alpha = row["confidence_alpha"]
    beta = row["confidence_beta"]
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "atom_type": row["atom_type"],
        "text_content": row["text_content"],
        "structured": json.loads(row["structured"]) if isinstance(row["structured"], str) else (row["structured"] or {}),
        "confidence_expected": alpha / (alpha + beta),
        "confidence_effective": row["confidence_effective"],
        "relevance_score": relevance_score,
        "source_type": row["source_type"],
        "domain_tags": list(row["domain_tags"]) if row["domain_tags"] else [],
        "created_at": row["created_at"],
        "last_accessed": row["last_accessed"],
        "access_count": row["access_count"],
        "is_active": row["is_active"],
        "confidence_alpha": alpha,
        "confidence_beta": beta,
    }
```

- [ ] **Step 5: Strip alpha/beta at non-full verbosity**

In `_apply_verbosity` (line 253), strip metadata when verbosity is not "full":

```python
def _apply_verbosity(atoms: list[dict], verbosity: str, max_chars: int) -> list[dict]:
    """Compress text_content according to verbosity mode."""
    if verbosity == "full":
        return atoms
    for atom in atoms:
        atom.pop("confidence_alpha", None)
        atom.pop("confidence_beta", None)
        text = atom["text_content"]
        if verbosity == "summary":
            end = text.find(". ")
            if end > 0:
                atom["text_content"] = text[: end + 1]
        elif verbosity == "truncated" and len(text) > max_chars:
            atom["text_content"] = text[:max_chars].rstrip() + "..."
    return atoms
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_confidence_metadata.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add tests/test_confidence_metadata.py mnemo/server/services/atom_service.py mnemo/server/models.py
git commit -m "feat: surface confidence metadata (alpha/beta) in recall at full verbosity"
```

---

## Task 2: Verify and Fix Bayesian Update Code Path

**Depends on:** Nothing

**Files:**
- Audit: `mnemo/server/services/atom_service.py:93-119` (`_merge_duplicate`)
- Test: `tests/test_bayesian_update.py`

### Pre-analysis

The code **already works correctly**. In `_merge_duplicate` (atom_service.py:93-119):
- `new_alpha = existing_alpha + incoming_alpha - 1.0` — Bayesian counting (subtracts the shared prior)
- `new_beta = existing_beta + incoming_beta - 1.0` — same
- Values are clamped to min 1.0
- Persisted via `UPDATE atoms SET confidence_alpha = $1, confidence_beta = $2`
- `access_count` is also incremented

The dedup threshold is 0.99 (config.py:14). The spec mentions 0.97 — we keep 0.99 since that's the production value.

### Steps

- [ ] **Step 1: Write the verification test**

```python
# tests/test_bayesian_update.py
import pytest

pytestmark = pytest.mark.anyio


async def test_bayesian_alpha_increments_on_duplicate_store(client, agent, pool):
    """Storing the same fact multiple times should increment alpha via Bayesian update."""
    agent_id = agent["id"]
    text = "The sky is blue."

    # Store the fact 3 times
    for _ in range(3):
        resp = await client.post(
            f"/v1/agents/{agent_id}/remember",
            json={"text": text},
        )
        assert resp.status_code == 201

    # Query the atoms table directly to check alpha
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT confidence_alpha, confidence_beta, access_count
            FROM atoms
            WHERE agent_id = $1 AND text_content LIKE '%sky is blue%'
            AND is_active = true
            ORDER BY confidence_alpha DESC
            LIMIT 1
            """,
            agent_id,
        )

    assert row is not None, "Atom not found"
    # Alpha should be greater than the initial prior (reinforced twice)
    assert row["confidence_alpha"] > 2.0, f"Alpha not incremented: {row['confidence_alpha']}"
    assert row["access_count"] >= 2, f"Access count not incremented: {row['access_count']}"


async def test_bayesian_update_persists_to_database(client, agent, pool):
    """Verify the Bayesian update is persisted, not just in-memory."""
    agent_id = agent["id"]
    text = "Water boils at 100 degrees Celsius."

    # Store twice
    await client.post(f"/v1/agents/{agent_id}/remember", json={"text": text})
    await client.post(f"/v1/agents/{agent_id}/remember", json={"text": text})

    # Read directly from DB
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT confidence_alpha
            FROM atoms
            WHERE agent_id = $1 AND text_content LIKE '%boils at 100%'
            AND is_active = true
            ORDER BY confidence_alpha DESC
            LIMIT 1
            """,
            agent_id,
        )

    assert row is not None
    initial_alpha = row["confidence_alpha"]

    # Store a third time
    await client.post(f"/v1/agents/{agent_id}/remember", json={"text": text})

    async with pool.acquire() as conn:
        row2 = await conn.fetchrow(
            """
            SELECT confidence_alpha
            FROM atoms
            WHERE agent_id = $1 AND text_content LIKE '%boils at 100%'
            AND is_active = true
            ORDER BY confidence_alpha DESC
            LIMIT 1
            """,
            agent_id,
        )

    assert row2["confidence_alpha"] >= initial_alpha
```

- [ ] **Step 2: Run test to verify it passes (this is a verification test, not TDD)**

Run: `uv run pytest tests/test_bayesian_update.py -v`
Expected: PASS — the code path already works

- [ ] **Step 3: Add a clarifying docstring to `_merge_duplicate`**

In `atom_service.py`, update the docstring of `_merge_duplicate` (line 93):

```python
async def _merge_duplicate(
    conn: asyncpg.Connection,
    existing_id: UUID,
    existing_alpha: float,
    existing_beta: float,
    incoming_alpha: float,
    incoming_beta: float,
) -> None:
    """Bayesian update: add evidence from the incoming atom into the existing one.

    Reinforcement increments alpha by (incoming_alpha - 1), the new evidence minus
    the shared prior. For a typical high-confidence duplicate with incoming
    Beta(8,1), this adds 7 to alpha per repetition. The update is persisted
    immediately and reflected in the next recall via effective_confidence().
    """
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_bayesian_update.py mnemo/server/services/atom_service.py
git commit -m "test: verify Bayesian alpha increment on duplicate store"
```

---

## Task 3: Mitigate the Super-Atom Problem

**Depends on:** Nothing

**Files:**
- Modify: `mnemo/server/services/atom_service.py` (extract `composite_score()`, apply in `retrieve`)
- Modify: `mnemo/server/services/view_service.py` (use shared `composite_score()`)
- Test: `tests/test_specificity_penalty.py`

### Pre-analysis

The `source_type` column already exists on atoms with `'consolidation'` as a value. No migration needed. The composite score `similarity * (0.7 + 0.3 * c_eff)` appears at:
1. `atom_service.py:667` — primary sort
2. `atom_service.py:678` — primary response score
3. `atom_service.py:717` — expanded atom score
4. `view_service.py:426` — shared view recall sort

The shared view recall query already SELECTs `source_type`, so no query changes needed.

### Steps

- [ ] **Step 1: Write the failing test**

```python
# tests/test_specificity_penalty.py
import pytest

pytestmark = pytest.mark.anyio


async def test_consolidated_atom_ranks_below_specific_atom(client, agent, pool):
    """A consolidated atom should rank below a decomposer atom due to specificity penalty."""
    agent_id = agent["id"]

    # Store a specific fact
    await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "The Hetzner server runs PostgreSQL 16 with pgvector."},
    )

    # Manually insert a consolidated atom (broad semantics)
    from mnemo.server.embeddings import encode
    broad_text = "Generalised from 5 observations: The server infrastructure uses various database technologies."
    embedding = await encode(broad_text)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO atoms (
                agent_id, atom_type, text_content, embedding,
                confidence_alpha, confidence_beta,
                source_type, domain_tags,
                decay_half_life_days, decay_type, decomposer_version
            ) VALUES ($1, 'semantic', $2, $3::vector, 8.0, 1.0,
                      'consolidation', '{}', 90.0, 'none', 'consolidation_v1')
            """,
            agent_id, broad_text, embedding,
        )

    # Recall — the specific atom should rank above the consolidated one
    resp = await client.post(
        f"/v1/agents/{agent_id}/recall",
        json={"query": "PostgreSQL database server", "max_results": 10},
    )
    assert resp.status_code == 200
    atoms = resp.json()["atoms"]
    specific = [a for a in atoms if "Hetzner" in a["text_content"]]
    consolidated = [a for a in atoms if "Generalised" in a["text_content"]]

    if specific and consolidated:
        specific_score = specific[0]["relevance_score"]
        consolidated_score = consolidated[0]["relevance_score"]
        assert consolidated_score < specific_score, (
            f"Consolidated atom ({consolidated_score:.3f}) should score lower "
            f"than specific atom ({specific_score:.3f})"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_specificity_penalty.py -v`
Expected: May FAIL depending on current similarity scores

- [ ] **Step 3: Extract a public helper for the composite score**

In `atom_service.py`, add a helper near line 275 (before `_row_to_atom_response`). Use a public name since `view_service.py` will import it:

```python
def composite_score(similarity: float, confidence_effective: float, source_type: str) -> float:
    """Composite ranking score with specificity penalty for consolidated atoms."""
    base = similarity * (0.7 + 0.3 * confidence_effective)
    if source_type == "consolidation":
        base *= 0.85  # 15% penalty — broad embeddings over-match
    return base
```

- [ ] **Step 4: Replace all composite score computations in atom_service.py**

In `retrieve()`, replace line 667 sort:
```python
rows.sort(
    key=lambda r: composite_score(r["cosine_sim"], r["confidence_effective"], r["source_type"]),
    reverse=True,
)
```

Replace line 678 primary_responses:
```python
primary_responses = [
    _row_to_atom_response(r, composite_score(r["cosine_sim"], r["confidence_effective"], r["source_type"]))
    for r in primary
]
```

Replace line 717 expanded score:
```python
score = composite_score(sim, r["confidence_effective"], r["source_type"])
```

- [ ] **Step 5: Update view_service.py composite score**

In `view_service.py`, import and use the shared helper:
```python
from .atom_service import composite_score
```

At line 426 (shared view recall sort):
```python
rows.sort(
    key=lambda r: composite_score(r["similarity"], r["confidence_effective"], r["source_type"]),
    reverse=True,
)
```

Check that the shared view recall query SELECTs `source_type`. If not, add it.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_specificity_penalty.py tests/test_recall_ranking.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add mnemo/server/services/atom_service.py mnemo/server/services/view_service.py tests/test_specificity_penalty.py
git commit -m "feat: add 15% specificity penalty for consolidated atoms in recall ranking"
```

---

## Task 4: Enrich `mnemo_stats` with Cold-Start Summary

**Depends on:** Nothing (access_count already exists)

**Files:**
- Modify: `mnemo/server/services/atom_service.py:820-890` (`get_agent_stats`)
- Modify: `mnemo/server/models.py:213-224` (`AgentStats`)
- Test: `tests/test_stats_enrichment.py`

### Steps

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stats_enrichment.py
import pytest

pytestmark = pytest.mark.anyio


async def test_stats_includes_topics(client, agent):
    """Stats should include top domain tags as topics."""
    agent_id = agent["id"]
    await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "PostgreSQL uses MVCC for concurrency.", "domain_tags": ["databases"]},
    )
    await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "FastAPI is built on Starlette.", "domain_tags": ["web-frameworks"]},
    )

    resp = await client.get(f"/v1/agents/{agent_id}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "topics" in data
    assert isinstance(data["topics"], list)


async def test_stats_includes_date_range(client, agent):
    """Stats should include the date range of stored atoms."""
    agent_id = agent["id"]
    await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "First memory stored today."},
    )

    resp = await client.get(f"/v1/agents/{agent_id}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "date_range" in data
    assert data["date_range"] is not None
    assert "earliest" in data["date_range"]
    assert "latest" in data["date_range"]


async def test_stats_includes_most_accessed(client, agent):
    """Stats should include top accessed atoms."""
    agent_id = agent["id"]
    await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "The Q1 revenue target is two million dollars ARR."},
    )
    for _ in range(3):
        await client.post(
            f"/v1/agents/{agent_id}/recall",
            json={"query": "Q1 revenue target"},
        )

    resp = await client.get(f"/v1/agents/{agent_id}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "most_accessed" in data
    assert isinstance(data["most_accessed"], list)


async def test_stats_empty_agent_has_null_enrichments(client, agent):
    """An agent with no atoms should have empty/null enrichment fields."""
    agent_id = agent["id"]
    resp = await client.get(f"/v1/agents/{agent_id}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["topics"] == []
    assert data["date_range"] is None
    assert data["most_accessed"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_stats_enrichment.py -v`
Expected: FAIL — `topics`, `date_range`, `most_accessed` not in response

- [ ] **Step 3: Add new fields to AgentStats model**

In `mnemo/server/models.py`:

```python
class AgentStats(BaseModel):
    agent_id: UUID
    total_atoms: int
    active_atoms: int
    atoms_by_type: dict[str, int]
    arc_atoms: int
    total_edges: int
    avg_effective_confidence: float
    active_views: int
    granted_capabilities: int
    received_capabilities: int
    address: Optional[str] = None
    # Cold-start enrichment fields
    topics: list[str] = []
    date_range: Optional[dict] = None  # {"earliest": "2026-03-09", "latest": "2026-03-24"}
    most_accessed: list[dict] = []     # [{"text": "...", "hits": N}]
```

- [ ] **Step 4: Add enrichment queries to `get_agent_stats`**

In `atom_service.py`, extend `get_agent_stats` after the existing `received_count` query (around line 872). Add these queries:

```python
    # ── Cold-start enrichment ──

    # Topics: top domain tags by frequency
    tag_rows = await conn.fetch(
        """
        SELECT unnest(domain_tags) AS tag, COUNT(*) AS cnt
        FROM atoms
        WHERE agent_id = $1 AND is_active = true AND domain_tags != '{}'
        GROUP BY tag
        ORDER BY cnt DESC
        LIMIT 8
        """,
        agent_id,
    )
    topics = [r["tag"] for r in tag_rows]

    # Date range
    date_row = await conn.fetchrow(
        """
        SELECT MIN(created_at)::date AS earliest, MAX(created_at)::date AS latest
        FROM atoms
        WHERE agent_id = $1 AND is_active = true
        """,
        agent_id,
    )
    date_range = None
    if date_row and date_row["earliest"] is not None:
        date_range = {
            "earliest": str(date_row["earliest"]),
            "latest": str(date_row["latest"]),
        }

    # Most accessed (top 3)
    accessed_rows = await conn.fetch(
        """
        SELECT text_content, access_count
        FROM atoms
        WHERE agent_id = $1 AND is_active = true AND access_count > 0
        ORDER BY access_count DESC
        LIMIT 3
        """,
        agent_id,
    )
    most_accessed = [
        {"text": r["text_content"][:60], "hits": r["access_count"]}
        for r in accessed_rows
    ]
```

Then add to the return dict:
```python
        "topics": topics,
        "date_range": date_range,
        "most_accessed": most_accessed,
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_stats_enrichment.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mnemo/server/services/atom_service.py mnemo/server/models.py tests/test_stats_enrichment.py
git commit -m "feat: enrich mnemo_stats with topics, date range, and most accessed"
```

---

## Task 5: Add Async Store Status Endpoint

**Depends on:** Nothing

**Files:**
- Modify: `schema.sql` (add `store_jobs` table DDL)
- Modify: `mnemo/server/routes/memory.py` (insert store_jobs row, add status endpoint)
- Modify: `mnemo/server/services/atom_service.py` (update `store_background` to track status)
- Modify: `mnemo/server/models.py` (add `StoreJobResponse`)
- Modify: `tests/conftest.py` (add `store_jobs` to `_CLEAN`)
- Test: `tests/test_store_status.py`

### Migration

There is **no auto-migration mechanism**. The `store_jobs` table must be created manually:

```sql
-- Run against both prod and test databases:
CREATE TABLE IF NOT EXISTS store_jobs (
    store_id        UUID PRIMARY KEY,
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    operator_id     UUID NOT NULL REFERENCES operators(id),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'decomposing', 'complete', 'failed')),
    atoms_created   INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_store_jobs_agent ON store_jobs (agent_id);
```

The user should run this themselves (requires DB access).

### Steps

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store_status.py
import uuid
import pytest

pytestmark = pytest.mark.anyio


async def test_store_status_complete(client, agent):
    """After storing, the status endpoint should return 'complete'."""
    agent_id = agent["id"]
    resp = await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "The deployment runs on Hetzner cloud infrastructure."},
    )
    assert resp.status_code == 201
    store_id = resp.json()["store_id"]

    # In test mode (sync_store_for_tests=True), store is already complete
    status_resp = await client.get(f"/v1/stores/{store_id}/status")
    assert status_resp.status_code == 200
    data = status_resp.json()
    assert data["store_id"] == store_id
    assert data["status"] == "complete"
    assert data["atoms_created"] >= 1


async def test_store_status_not_found(client):
    """Querying a non-existent store_id should return 404."""
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/v1/stores/{fake_id}/status")
    assert resp.status_code == 404


async def test_store_status_failed(client, agent, pool):
    """A failed store should report status='failed'."""
    agent_id = agent["id"]
    store_id = uuid.uuid4()

    # Get operator_id for FK
    async with pool.acquire() as conn:
        op_row = await conn.fetchrow(
            "SELECT operator_id FROM agents WHERE id = $1", agent_id,
        )
        await conn.execute(
            """
            INSERT INTO store_jobs (store_id, agent_id, operator_id, status, error)
            VALUES ($1, $2, $3, 'failed', 'Test error message')
            """,
            store_id, agent_id, op_row["operator_id"],
        )

    resp = await client.get(f"/v1/stores/{store_id}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert "error" in data


async def test_store_status_wrong_operator_returns_404(client, pool):
    """A different operator should get 404, not 403 (don't leak existence)."""
    # Create a second operator + agent
    async with pool.acquire() as conn:
        op2 = await conn.fetchrow(
            "INSERT INTO operators (name) VALUES ('other-op') RETURNING id",
        )
        agent2 = await conn.fetchrow(
            "INSERT INTO agents (operator_id, name, domain_tags) VALUES ($1, 'other-agent', '{}') RETURNING id",
            op2["id"],
        )
        store_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO store_jobs (store_id, agent_id, operator_id, status)
            VALUES ($1, $2, $3, 'complete')
            """,
            store_id, agent2["id"], op2["id"],
        )

    # The default test client authenticates as the first operator.
    # Querying op2's store job should return 404.
    resp = await client.get(f"/v1/stores/{store_id}/status")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_status.py -v`
Expected: FAIL — endpoint and table don't exist

- [ ] **Step 3: Add `store_jobs` table to schema.sql**

Append the DDL from the Migration section above to `schema.sql`.

- [ ] **Step 4: Create the table in the test database**

The user runs the migration SQL against the test database themselves.

- [ ] **Step 5: Add `store_jobs` to conftest.py cleanup**

In `tests/conftest.py`, add `DELETE FROM store_jobs;` to `_CLEAN`, after `DELETE FROM store_failures;` (it has FKs to agents and operators):

```python
_CLEAN = """
DELETE FROM agent_trust;
DELETE FROM capabilities;
DELETE FROM snapshot_atoms;
DELETE FROM edges;
DELETE FROM views;
DELETE FROM store_failures;
DELETE FROM store_jobs;
DELETE FROM decomposer_usage;
DELETE FROM atoms;
DELETE FROM api_keys;
DELETE FROM agent_addresses;
DELETE FROM agents;
DELETE FROM operations;
DELETE FROM operators;
"""
```

- [ ] **Step 6: Add StoreJobResponse model**

In `mnemo/server/models.py`:

```python
class StoreJobResponse(BaseModel):
    store_id: UUID
    status: str
    atoms_created: int = 0
    created_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
```

- [ ] **Step 7: Update `store_background` to track job status**

In `atom_service.py`, modify `store_background` (line 509). The key changes:
1. Update status to `'decomposing'` before calling `store_from_text`
2. Update status to `'complete'` with atom count on success
3. Update status to `'failed'` with error on failure

```python
async def store_background(
    pool: asyncpg.Pool,
    store_id: UUID,
    agent_id: UUID,
    text: str,
    domain_tags: list[str],
    operator_id: UUID | None = None,
    remembered_on: datetime | None = None,
) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE store_jobs SET status = 'decomposing' WHERE store_id = $1",
                store_id,
            )
            result = await store_from_text(
                conn, agent_id, text, domain_tags,
                store_id=store_id, operator_id=operator_id,
                remembered_on=remembered_on,
            )
            await conn.execute(
                """
                UPDATE store_jobs
                SET status = 'complete', atoms_created = $1, completed_at = now()
                WHERE store_id = $2
                """,
                result["atoms_created"],
                store_id,
            )
    except Exception:
        error_msg = traceback.format_exc()
        logger.error("Background store %s failed: %s", store_id, error_msg)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE store_jobs
                    SET status = 'failed', error = $1, completed_at = now()
                    WHERE store_id = $2
                    """,
                    error_msg, store_id,
                )
                await conn.execute(
                    """
                    INSERT INTO store_failures (id, agent_id, original_text, error)
                    VALUES ($1, $2, $3, $4)
                    """,
                    store_id, agent_id, text, error_msg,
                )
        except Exception:
            logger.error("Failed to log store failure %s", store_id)
```

- [ ] **Step 8: Insert store_jobs row in the remember handler**

In `routes/memory.py`, modify the `remember()` function. After generating `store_id` and within the existing `async with get_conn()` block, insert the store_jobs row:

```python
    store_id = uuid4()
    async with get_conn() as conn:
        await log_operation(conn, "remember", operator["id"], target_id=agent_uuid)
        await conn.execute(
            """
            INSERT INTO store_jobs (store_id, agent_id, operator_id)
            VALUES ($1, $2, $3)
            """,
            store_id, agent_uuid, operator_id,
        )
```

- [ ] **Step 9: Add the status endpoint**

In `routes/memory.py`, add the GET endpoint. Note: the router is mounted at `/v1`, so the route path should be `/stores/{store_id}/status` (not `/v1/stores/...`):

```python
from ..models import RememberRequest, RememberResponse, RetrieveRequest, RetrieveResponse, StoreJobResponse

@router.get("/stores/{store_id}/status", response_model=StoreJobResponse)
async def store_status(store_id: UUID, operator=Depends(get_current_operator)):
    """Check the status of an async store operation."""
    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT sj.store_id, sj.status, sj.atoms_created,
                   sj.created_at, sj.completed_at, sj.error
            FROM store_jobs sj
            JOIN agents a ON a.id = sj.agent_id
            WHERE sj.store_id = $1 AND a.operator_id = $2
            """,
            store_id, operator["id"],
        )
    if not row:
        raise HTTPException(status_code=404, detail="Store job not found")
    return dict(row)
```

- [ ] **Step 10: Run tests**

Run: `uv run pytest tests/test_store_status.py -v`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add schema.sql mnemo/server/models.py mnemo/server/routes/memory.py mnemo/server/services/atom_service.py tests/conftest.py tests/test_store_status.py
git commit -m "feat: add store job tracking and GET /v1/stores/{store_id}/status endpoint"
```

---

## Task 6: Tighten Input Validation

**Depends on:** Nothing

**Files:**
- Modify: `mnemo/server/routes/memory.py:18-48` (add validation to `remember` handler)
- Test: `tests/test_input_validation.py`

### Steps

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_input_validation.py
import pytest

pytestmark = pytest.mark.anyio


async def test_reject_empty_text(client, agent):
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": ""},
    )
    assert resp.status_code == 422


async def test_reject_whitespace_only_text(client, agent):
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "   \n\t  "},
    )
    assert resp.status_code == 422


async def test_reject_text_shorter_than_3_chars(client, agent):
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "ab"},
    )
    assert resp.status_code == 422


async def test_accept_text_exactly_3_chars(client, agent):
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "abc"},
    )
    assert resp.status_code == 201


async def test_reject_text_exceeding_max_length(client, agent):
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "x" * 50_001},
    )
    assert resp.status_code == 413


async def test_accept_text_at_max_length(client, agent):
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "x" * 50_000},
    )
    assert resp.status_code == 201


async def test_accept_long_text_with_warning(client, agent):
    """Text between 10K and 50K should be accepted."""
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "x" * 15_000},
    )
    assert resp.status_code == 201
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_input_validation.py -v`
Expected: FAIL — empty/short text currently accepted

- [ ] **Step 3: Add validation to the remember handler**

In `routes/memory.py`, add validation at the top of `remember()`, after the `pool = await get_pool()` line but before agent resolution:

```python
import logging

logger = logging.getLogger(__name__)

@router.post("/agents/{agent_id}/remember", response_model=RememberResponse, status_code=201)
async def remember(agent_id: str, body: RememberRequest, operator=Depends(get_current_operator)):
    """Store a free-text memory. Returns immediately; decomposition runs in background."""
    # ── Input validation ──
    stripped = body.text.strip()
    if not stripped:
        raise HTTPException(status_code=422, detail="text must contain non-whitespace content")
    if len(stripped) < 3:
        raise HTTPException(status_code=422, detail="text must be at least 3 characters")
    if len(body.text) > 50_000:
        raise HTTPException(status_code=413, detail=(
            "text exceeds maximum length of 50,000 characters. "
            "Split large documents into smaller sections before storing."
        ))
    if len(body.text) > 10_000:
        logger.warning("Large input: %d chars from agent %s", len(body.text), agent_id)

    # ...rest of handler unchanged...
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_input_validation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mnemo/server/routes/memory.py tests/test_input_validation.py
git commit -m "feat: add input validation for /remember — reject empty, short, and oversized text"
```

---

## Summary

| Task | Description | Migration? | Files Changed |
|------|-------------|-----------|---------------|
| 1 | Confidence metadata in recall | No | atom_service.py, models.py |
| 2 | Verify Bayesian update | No | atom_service.py (docstring only) |
| 3 | Super-atom specificity penalty | No | atom_service.py, view_service.py |
| 4 | Stats enrichment | No | atom_service.py, models.py |
| 5 | Store status endpoint | Yes (`store_jobs` table — manual migration) | schema.sql, routes/memory.py, atom_service.py, models.py, conftest.py |
| 6 | Input validation | No | routes/memory.py |
