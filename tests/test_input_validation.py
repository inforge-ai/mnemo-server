import pytest


async def test_reject_empty_text(client, agent):
    ag_headers = {"X-Agent-Key": agent["agent_key"]}
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": ""},
        headers=ag_headers,
    )
    assert resp.status_code == 422


async def test_reject_whitespace_only_text(client, agent):
    ag_headers = {"X-Agent-Key": agent["agent_key"]}
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "   \n\t  "},
        headers=ag_headers,
    )
    assert resp.status_code == 422


async def test_reject_text_shorter_than_3_chars(client, agent):
    ag_headers = {"X-Agent-Key": agent["agent_key"]}
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "ab"},
        headers=ag_headers,
    )
    assert resp.status_code == 422


async def test_accept_text_exactly_3_chars(client, agent):
    ag_headers = {"X-Agent-Key": agent["agent_key"]}
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "abc"},
        headers=ag_headers,
    )
    assert resp.status_code == 201


async def test_reject_text_exceeding_max_length(client, agent):
    ag_headers = {"X-Agent-Key": agent["agent_key"]}
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "x" * 50_001},
        headers=ag_headers,
    )
    assert resp.status_code in (413, 422)  # Pydantic max_length (422) or route check (413)


async def test_accept_text_at_max_length(client, agent):
    ag_headers = {"X-Agent-Key": agent["agent_key"]}
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "x" * 50_000},
        headers=ag_headers,
    )
    assert resp.status_code == 201


async def test_accept_long_text_with_warning(client, agent):
    """Text between 10K and 50K should be accepted."""
    ag_headers = {"X-Agent-Key": agent["agent_key"]}
    resp = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "x" * 15_000},
        headers=ag_headers,
    )
    assert resp.status_code == 201
