import pytest


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
