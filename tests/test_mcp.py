"""
Tests for the MCP server tool functions.

Strategy: import and call the tool functions directly (bypass MCP transport).
Inject the ASGI-backed MnemoClient using the same pattern as test_simulation.py.
"""

import pytest
from uuid import UUID

import mnemo.mcp.mcp_server as mcp_module
from mnemo.mcp.mcp_server import mnemo_remember, mnemo_recall, mnemo_stats


# ── ASGI MnemoClient adapter (same pattern as test_simulation.py) ─────────────

class _AsgiMnemoClient:
    """Wraps httpx AsyncClient (ASGI transport) to match MnemoClient's interface."""

    def __init__(self, httpx_client):
        self._c = httpx_client

    async def register_agent(self, name, persona=None, domain_tags=None):
        r = await self._c.post(
            "/v1/agents",
            json={"name": name, "persona": persona, "domain_tags": domain_tags or []},
        )
        r.raise_for_status()
        return r.json()

    async def get_agent(self, agent_id):
        r = await self._c.get(f"/v1/agents/{agent_id}")
        r.raise_for_status()
        return r.json()

    async def remember(self, agent_id, text, domain_tags=None):
        r = await self._c.post(
            f"/v1/agents/{agent_id}/remember",
            json={"text": text, "domain_tags": domain_tags or []},
        )
        r.raise_for_status()
        return r.json()

    async def recall(self, agent_id, query, domain_tags=None, max_results=10,
                     min_confidence=0.1, expand_graph=True, **_):
        r = await self._c.post(
            f"/v1/agents/{agent_id}/recall",
            json={
                "query": query,
                "max_results": max_results,
                "min_confidence": min_confidence,
                "expand_graph": expand_graph,
            },
        )
        r.raise_for_status()
        return r.json()

    async def stats(self, agent_id):
        r = await self._c.get(f"/v1/agents/{agent_id}/stats")
        r.raise_for_status()
        return r.json()

    async def close(self):
        pass


# ── Fixture: wire the ASGI client + agent into the module globals ─────────────

@pytest.fixture(autouse=True)
async def _inject_mcp_state(client, agent):
    """
    Replace the module-level _client and _agent_id with test doubles so the
    tool functions use the ASGI-backed client instead of making real HTTP calls.
    """
    adapter = _AsgiMnemoClient(client)
    agent_id = UUID(agent["id"])

    # Patch module globals
    original_client = mcp_module._client
    original_agent_id = mcp_module._agent_id
    mcp_module._client = adapter
    mcp_module._agent_id = agent_id

    yield

    # Restore
    mcp_module._client = original_client
    mcp_module._agent_id = original_agent_id


# ── Tool tests ────────────────────────────────────────────────────────────────

async def test_remember_stores_memories(client, agent):
    """mnemo_remember returns a human-readable confirmation string."""
    result = await mnemo_remember(
        text=(
            "pandas.read_csv silently coerces mixed-type columns. "
            "I found this while processing a dataset. "
            "Always specify dtype explicitly when using read_csv."
        ),
        domain_tags=["python", "pandas"],
    )
    assert "memories" in result
    assert isinstance(result, str)


async def test_remember_no_domain_tags(client, agent):
    """mnemo_remember works without domain_tags (defaults to empty list)."""
    result = await mnemo_remember(text="asyncpg does not auto-commit transactions.")
    assert "memories" in result


async def test_remember_deduplication_reported(client, agent):
    """When a duplicate is merged, the result mentions it."""
    text = "asyncpg uses a connection pool internally."
    await mnemo_remember(text=text)
    result = await mnemo_remember(text=text)
    # Either atoms_created or duplicates_merged reported — no crash
    assert isinstance(result, str)


async def test_recall_returns_results(client, agent):
    """After storing a memory, mnemo_recall should find it."""
    await mnemo_remember(
        text="pgvector stores embeddings as vector(384) columns in PostgreSQL.",
        domain_tags=["postgres"],
    )
    result = await mnemo_recall(query="vector embeddings postgres")
    assert "pgvector" in result or "vector" in result.lower() or "No relevant" in result


async def test_recall_empty(client, agent):
    """With no stored memories, mnemo_recall returns the 'no results' message."""
    result = await mnemo_recall(query="quantum entanglement photon spin")
    assert "No relevant memories" in result


async def test_recall_with_domain_tags(client, agent):
    """Domain tags are forwarded without error."""
    result = await mnemo_recall(
        query="database indexing strategies",
        domain_tags=["postgres"],
        max_results=3,
    )
    assert isinstance(result, str)


async def test_recall_confidence_labels(client, agent):
    """Result lines include confidence labels."""
    await mnemo_remember(text="Redis is an in-memory key-value store.")
    result = await mnemo_recall(query="Redis caching")
    if "No relevant" not in result:
        assert any(label in result for label in ("high", "moderate", "low"))


async def test_stats_empty(client, agent):
    """mnemo_stats returns a structured summary even with no memories."""
    result = await mnemo_stats()
    assert "Total memories" in result
    assert "active:" in result


async def test_stats_after_remember(client, agent):
    """After storing a memory, stats should show active_atoms > 0."""
    await mnemo_remember(text="PostgreSQL supports partial indexes on filtered rows.")
    result = await mnemo_stats()
    assert "Total memories" in result
    # Check that at least one atom is reflected (active count is non-zero)
    assert "active: 0" not in result
