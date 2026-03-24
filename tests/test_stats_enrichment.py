import pytest


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
