"""
Tests for the shared MnemoClient production fixes (Part 3 of build spec).
Uses respx to mock httpx at the transport layer — no real server needed.
"""

import asyncio
import pytest
import respx
import httpx

from mnemo_client import (
    MnemoClient,
    MnemoClientSync,
    MnemoAuthError,
    MnemoNotFoundError,
    MnemoServerError,
    RememberResult,
    RecallResult,
)

BASE = "https://test.mnemo.ai"
KEY = "test-key-abc"
AGENT = "00000000-0000-0000-0000-000000000001"


# ── Construction ───────────────────────────────────────────────────────────────

def test_api_key_required_empty():
    with pytest.raises(ValueError, match="api_key is required"):
        MnemoClient(BASE, api_key="")


def test_api_key_required_none():
    with pytest.raises(ValueError, match="api_key is required"):
        MnemoClient(BASE, api_key=None)


def test_api_key_required_missing():
    with pytest.raises((ValueError, TypeError)):
        MnemoClient(BASE)


# ── Auth header ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_auth_header_on_every_request():
    route = respx.post(f"{BASE}/v1/agents/{AGENT}/remember").mock(
        return_value=httpx.Response(200, json={
            "atoms_created": 1, "edges_created": 0, "duplicates_merged": 0
        })
    )
    async with MnemoClient(BASE, api_key=KEY) as client:
        await client.remember(AGENT, "test memory")

    assert route.called
    req = route.calls[0].request
    assert req.headers["authorization"] == f"Bearer {KEY}"


# ── Typed return values ────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_remember_returns_typed_dict():
    respx.post(f"{BASE}/v1/agents/{AGENT}/remember").mock(
        return_value=httpx.Response(200, json={
            "atoms_created": 2, "edges_created": 1, "duplicates_merged": 0
        })
    )
    async with MnemoClient(BASE, api_key=KEY) as client:
        result = await client.remember(AGENT, "some memory")

    assert result["atoms_created"] == 2
    assert result["edges_created"] == 1
    assert result["duplicates_merged"] == 0


@pytest.mark.asyncio
@respx.mock
async def test_recall_returns_typed_dict():
    respx.post(f"{BASE}/v1/agents/{AGENT}/recall").mock(
        return_value=httpx.Response(200, json={
            "atoms": [
                {
                    "id": AGENT,
                    "atom_type": "semantic",
                    "text_content": "test",
                    "confidence_expected": 0.8,
                    "confidence_effective": 0.7,
                    "domain_tags": ["python"],
                    "source_type": "direct_experience",
                    "created_at": "2026-01-01T00:00:00Z",
                    "access_count": 3,
                }
            ],
            "expanded_atoms": [],
            "total_retrieved": 1,
        })
    )
    async with MnemoClient(BASE, api_key=KEY) as client:
        result = await client.recall(AGENT, "test query")

    assert result["total_retrieved"] == 1
    assert len(result["atoms"]) == 1
    assert result["atoms"][0]["atom_type"] == "semantic"


# ── Exception hierarchy ────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_401_raises_mnemo_auth_error():
    respx.get(f"{BASE}/v1/health").mock(return_value=httpx.Response(401))
    async with MnemoClient(BASE, api_key=KEY) as client:
        with pytest.raises(MnemoAuthError):
            await client.health()


@pytest.mark.asyncio
@respx.mock
async def test_403_raises_mnemo_auth_error():
    respx.post(f"{BASE}/v1/agents/{AGENT}/remember").mock(
        return_value=httpx.Response(403, text="Forbidden")
    )
    async with MnemoClient(BASE, api_key=KEY) as client:
        with pytest.raises(MnemoAuthError):
            await client.remember(AGENT, "test")


@pytest.mark.asyncio
@respx.mock
async def test_404_raises_mnemo_not_found_error():
    respx.get(f"{BASE}/v1/agents/{AGENT}").mock(
        return_value=httpx.Response(404, text="Not found")
    )
    async with MnemoClient(BASE, api_key=KEY) as client:
        with pytest.raises(MnemoNotFoundError):
            await client.get_agent(AGENT)


@pytest.mark.asyncio
@respx.mock
async def test_500_raises_mnemo_server_error():
    respx.post(f"{BASE}/v1/agents/{AGENT}/remember").mock(
        return_value=httpx.Response(500, text="Internal error")
    )
    async with MnemoClient(BASE, api_key=KEY) as client:
        with pytest.raises(MnemoServerError):
            await client.remember(AGENT, "test")


# ── UUID normalisation ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_uuid_object_accepted():
    import uuid
    agent_uuid = uuid.UUID(AGENT)
    route = respx.post(f"{BASE}/v1/agents/{AGENT}/remember").mock(
        return_value=httpx.Response(200, json={
            "atoms_created": 1, "edges_created": 0, "duplicates_merged": 0
        })
    )
    async with MnemoClient(BASE, api_key=KEY) as client:
        # Should not raise TypeError — UUID object converted to string
        result = await client.remember(agent_uuid, "test memory")
    assert route.called
    assert result["atoms_created"] == 1


# ── MnemoClientSync ────────────────────────────────────────────────────────────

@respx.mock
def test_sync_client_remember():
    respx.post(f"{BASE}/v1/agents/{AGENT}/remember").mock(
        return_value=httpx.Response(200, json={
            "atoms_created": 1, "edges_created": 0, "duplicates_merged": 0
        })
    )
    client = MnemoClientSync(api_key=KEY, agent_id=AGENT, base_url=BASE)
    result = client.remember("test memory in sync context")
    assert result["atoms_created"] == 1


@respx.mock
def test_sync_client_in_running_loop():
    """MnemoClientSync must work when called from within an asyncio.run() context."""
    respx.post(f"{BASE}/v1/agents/{AGENT}/remember").mock(
        return_value=httpx.Response(200, json={
            "atoms_created": 1, "edges_created": 0, "duplicates_merged": 0
        })
    )
    client = MnemoClientSync(api_key=KEY, agent_id=AGENT, base_url=BASE)

    # Simulate being called from inside an async context
    async def inner():
        return client.remember("called from running loop")

    result = asyncio.run(inner())
    assert result["atoms_created"] == 1
