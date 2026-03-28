"""
Integration tests for the full admin feature set (Phase 5).

Covers: health endpoints, operator CRUD, agent admin, trust admin,
and sharing enforcement on grant/recall paths.
"""

import os

import pytest
from httpx import AsyncClient

from tests.conftest import TEST_ADMIN_KEY, admin_headers

from mnemo.server.config import settings

ADMIN_TOKEN = TEST_ADMIN_KEY

# Ensure settings has the admin key for all tests in this module.
_original_admin_key = settings.admin_key
settings.admin_key = ADMIN_TOKEN


def _admin(headers=None):
    h = admin_headers()
    if headers:
        h.update(headers)
    return h


async def _create_operator(client, username="alice", org="testorg",
                           display_name="Alice Smith", email="alice@test.com"):
    """Helper: create operator via admin API, return response data."""
    resp = await client.post("/v1/admin/operators", headers=_admin(), json={
        "username": username, "org": org, "display_name": display_name, "email": email,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_operator_and_agent(client, username="opadm", agent_name="managed"):
    """Helper: create operator + agent, return (op_data, api_key, agent_data)."""
    op_data = await _create_operator(
        client, username=username, org="testorg",
        display_name=f"Op {username}", email=f"{username}@test.com",
    )
    api_key = op_data["api_key"]
    auth = {"X-Operator-Key": api_key}
    agent_resp = await client.post(
        "/v1/agents", json={"name": agent_name, "domain_tags": ["test"]}, headers=auth,
    )
    assert agent_resp.status_code == 201, agent_resp.text
    return op_data, api_key, agent_resp.json()


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHealthEndpoints:
    async def test_health_basic(self, client):
        resp = await client.get("/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "uptime_seconds" in data
        assert data["postgres"] == "ok"

    async def test_health_detailed_requires_admin(self, client):
        resp = await client.get("/v1/health/detailed")
        assert resp.status_code in (401, 403)  # 401 for missing creds, 403 for wrong role

    async def test_health_detailed_with_admin(self, client):
        resp = await client.get("/v1/health/detailed", headers=_admin())
        assert resp.status_code == 200
        data = resp.json()
        assert "operator_count" in data
        assert "agent_count" in data
        assert "atom_count" in data
        assert "embedding_model" in data


# ---------------------------------------------------------------------------
# Operator CRUD
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestOperatorCRUD:
    async def test_create_operator(self, client):
        resp = await client.post("/v1/admin/operators", headers=_admin(), json={
            "username": "alice", "org": "testorg",
            "display_name": "Alice Smith", "email": "alice@test.com",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"] == "alice"
        assert data["org"] == "testorg"
        assert "api_key" in data
        assert data["api_key"].startswith("mnemo_")

    async def test_create_operator_duplicate(self, client):
        body = {"username": "bob", "org": "testorg",
                "display_name": "Bob", "email": "bob@test.com"}
        await client.post("/v1/admin/operators", headers=_admin(), json=body)
        resp = await client.post("/v1/admin/operators", headers=_admin(), json=body)
        assert resp.status_code == 409

    async def test_create_operator_bad_username(self, client):
        resp = await client.post("/v1/admin/operators", headers=_admin(), json={
            "username": "UPPER", "org": "testorg",
            "display_name": "Bad", "email": "bad@test.com",
        })
        assert resp.status_code == 422

    async def test_list_operators(self, client):
        await _create_operator(client, username="listtest", email="list@test.com")
        resp = await client.get("/v1/admin/operators", headers=_admin())
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["operators"]) >= 1

    async def test_get_operator(self, client):
        op = await _create_operator(client, username="gettest", email="get@test.com")
        op_id = op["uuid"]
        resp = await client.get(f"/v1/admin/operators/{op_id}", headers=_admin())
        assert resp.status_code == 200
        assert resp.json()["username"] == "gettest"

    async def test_suspend_operator(self, client):
        op_data, api_key, agent_data = await _create_operator_and_agent(
            client, username="susptest", agent_name="agent-susp",
        )
        op_id = op_data["uuid"]

        resp = await client.post(
            f"/v1/admin/operators/{op_id}/suspend", headers=_admin(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "suspended"
        assert resp.json()["agents_departed"] >= 1

    async def test_reinstate_operator_agents_stay_departed(self, client):
        op_data, api_key, agent_data = await _create_operator_and_agent(
            client, username="reintest", agent_name="agent-rein",
        )
        op_id = op_data["uuid"]

        await client.post(f"/v1/admin/operators/{op_id}/suspend", headers=_admin())
        await client.post(f"/v1/admin/operators/{op_id}/reinstate", headers=_admin())

        # Check agent is still departed via operator detail endpoint
        op_resp = await client.get(
            f"/v1/admin/operators/{op_id}", headers=_admin(),
        )
        agents = op_resp.json()["agents"]
        departed = [a for a in agents if a["status"] == "departed"]
        assert len(departed) >= 1

    async def test_rotate_key(self, client):
        op = await _create_operator(client, username="keytest", email="key@test.com")
        op_id = op["uuid"]
        old_key = op["api_key"]

        resp = await client.post(
            f"/v1/admin/operators/{op_id}/rotate-key", headers=_admin(),
        )
        assert resp.status_code == 200
        new_key = resp.json()["api_key"]
        assert new_key != old_key
        assert new_key.startswith("mnemo_")


# ---------------------------------------------------------------------------
# Agent admin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAgentAdmin:
    async def test_list_agents(self, client):
        await _create_operator_and_agent(client, username="aglist")
        resp = await client.get("/v1/admin/agents", headers=_admin())
        assert resp.status_code == 200
        data = resp.json()
        agents = data["agents"]
        assert isinstance(agents, list)
        assert len(agents) >= 1

    async def test_depart_then_check_status(self, client):
        """Depart an agent, then verify status via operator detail endpoint."""
        op_data, api_key, agent_data = await _create_operator_and_agent(
            client, username="agfilt",
        )
        await client.post(
            f"/v1/admin/agents/{agent_data['id']}/depart", headers=_admin(),
        )

        # Verify via operator detail endpoint (returns agents with status)
        op_resp = await client.get(
            f"/v1/admin/operators/{op_data['uuid']}", headers=_admin(),
        )
        agents = op_resp.json()["agents"]
        departed = [a for a in agents if a["status"] == "departed"]
        assert len(departed) >= 1

    async def test_admin_depart_agent(self, client):
        _, _, agent_data = await _create_operator_and_agent(
            client, username="agdep",
        )
        resp = await client.post(
            f"/v1/admin/agents/{agent_data['id']}/depart", headers=_admin(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "departed"

    async def test_admin_reinstate_agent(self, client):
        _, _, agent_data = await _create_operator_and_agent(
            client, username="agrein",
        )
        await client.post(
            f"/v1/admin/agents/{agent_data['id']}/depart", headers=_admin(),
        )
        resp = await client.post(
            f"/v1/admin/agents/{agent_data['id']}/reinstate", headers=_admin(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    async def test_reinstate_fails_if_operator_suspended(self, client):
        op_data, api_key, agent_data = await _create_operator_and_agent(
            client, username="agopsusp",
        )
        op_id = op_data["uuid"]

        # Suspend operator (departs all agents)
        await client.post(
            f"/v1/admin/operators/{op_id}/suspend", headers=_admin(),
        )
        # Try reinstating agent while operator is suspended
        resp = await client.post(
            f"/v1/admin/agents/{agent_data['id']}/reinstate", headers=_admin(),
        )
        assert resp.status_code == 409


    async def test_admin_rotate_agent_key(self, client):
        """Admin can rotate an agent's key; new key works, old one doesn't."""
        _, _, agent_data = await _create_operator_and_agent(client, username="agrotate")
        agent_id = agent_data["id"]
        old_key = agent_data["agent_key"]

        # Verify old key works
        resp = await client.get(
            f"/v1/agents/{agent_id}/stats",
            headers={"X-Agent-Key": old_key},
        )
        assert resp.status_code == 200

        # Rotate via admin endpoint
        resp = await client.post(
            f"/v1/admin/agents/{agent_id}/rotate-key", headers=_admin(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_key" in data
        new_key = data["agent_key"]
        assert new_key != old_key

        # New key works
        resp = await client.get(
            f"/v1/agents/{agent_id}/stats",
            headers={"X-Agent-Key": new_key},
        )
        assert resp.status_code == 200

        # Old key is invalid
        resp = await client.get(
            f"/v1/agents/{agent_id}/stats",
            headers={"X-Agent-Key": old_key},
        )
        assert resp.status_code == 401

    async def test_operator_rotate_agent_key(self, client):
        """Operator can rotate their own agent's key."""
        _, api_key, agent_data = await _create_operator_and_agent(client, username="oprotate")
        agent_id = agent_data["id"]
        old_key = agent_data["agent_key"]

        # Rotate via operator endpoint
        resp = await client.post(
            f"/v1/agents/{agent_id}/rotate-key",
            headers={"X-Operator-Key": api_key},
        )
        assert resp.status_code == 200
        data = resp.json()
        new_key = data["agent_key"]
        assert new_key != old_key

        # New key works
        resp = await client.get(
            f"/v1/agents/{agent_id}/stats",
            headers={"X-Agent-Key": new_key},
        )
        assert resp.status_code == 200

    async def test_operator_cannot_rotate_other_operators_agent(self, client):
        """Operator A cannot rotate operator B's agent key."""
        _, _, agent_data = await _create_operator_and_agent(client, username="oprotowner")
        other_op = await _create_operator(client, username="oprotother")
        other_key = other_op["api_key"]

        resp = await client.post(
            f"/v1/agents/{agent_data['id']}/rotate-key",
            headers={"X-Operator-Key": other_key},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Trust admin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTrustAdmin:
    async def test_trust_status(self, client):
        resp = await client.get("/v1/admin/trust/status", headers=_admin())
        assert resp.status_code == 200
        assert "sharing_enabled" in resp.json()

    async def test_trust_disable_enable(self, client):
        # Disable
        resp = await client.post("/v1/admin/trust/disable", headers=_admin())
        assert resp.status_code == 200
        assert resp.json()["sharing_enabled"] is False

        # Verify
        status_resp = await client.get(
            "/v1/admin/trust/status", headers=_admin(),
        )
        assert status_resp.json()["sharing_enabled"] is False

        # Re-enable
        resp = await client.post("/v1/admin/trust/enable", headers=_admin())
        assert resp.status_code == 200
        assert resp.json()["sharing_enabled"] is True

    async def test_list_shares_empty(self, client):
        resp = await client.get("/v1/admin/trust/shares", headers=_admin())
        assert resp.status_code == 200
        assert resp.json()["shares"] == []


# ---------------------------------------------------------------------------
# Sharing enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSharingEnforcement:
    """Test that global sharing toggle is enforced on grant and shared recall."""

    async def test_grant_blocked_when_sharing_disabled(self, client, pool):
        """When sharing is disabled, granting a capability should fail with 403."""
        # Create operator + 2 agents
        op_data = await _create_operator(
            client, username="shareop", email="shareop@test.com",
        )
        api_key = op_data["api_key"]
        op_auth = {"X-Operator-Key": api_key}

        a1 = await client.post(
            "/v1/agents",
            json={"name": "granter", "domain_tags": ["test"]},
            headers=op_auth,
        )
        a2 = await client.post(
            "/v1/agents",
            json={"name": "grantee", "domain_tags": ["test"]},
            headers=op_auth,
        )
        assert a1.status_code == 201
        assert a2.status_code == 201
        agent1_id = a1.json()["id"]
        agent2_id = a2.json()["id"]
        ag1_auth = {"X-Agent-Key": a1.json()["agent_key"]}

        # Remember something to create atoms
        await client.post(
            f"/v1/agents/{agent1_id}/remember",
            json={"text": "Test memory for sharing enforcement."},
            headers=ag1_auth,
        )

        # Create a view
        view_resp = await client.post(
            f"/v1/agents/{agent1_id}/views",
            json={"name": "test-view", "atom_filter": {"domain_tags": ["test"]}},
            headers=ag1_auth,
        )
        assert view_resp.status_code == 201, view_resp.text
        view_id = view_resp.json()["id"]

        # Disable sharing
        await client.post("/v1/admin/trust/disable", headers=_admin())

        # Try to grant -- should fail
        grant_resp = await client.post(
            f"/v1/agents/{agent1_id}/grant",
            json={"view_id": view_id, "grantee_id": agent2_id},
            headers=ag1_auth,
        )
        assert grant_resp.status_code == 403
        assert "disabled" in grant_resp.json()["detail"].lower()

        # Re-enable sharing
        await client.post("/v1/admin/trust/enable", headers=_admin())

        # Grant should now succeed
        grant_resp2 = await client.post(
            f"/v1/agents/{agent1_id}/grant",
            json={"view_id": view_id, "grantee_id": agent2_id},
            headers=ag1_auth,
        )
        assert grant_resp2.status_code == 201

    async def test_shared_recall_returns_empty_when_sharing_disabled(self, client, pool):
        """When sharing is disabled, shared recall should return empty results."""
        op_data = await _create_operator(
            client, username="recallop", email="recallop@test.com",
        )
        api_key = op_data["api_key"]
        op_auth = {"X-Operator-Key": api_key}

        a1 = await client.post(
            "/v1/agents",
            json={"name": "recaller", "domain_tags": ["test"]},
            headers=op_auth,
        )
        assert a1.status_code == 201
        agent1_id = a1.json()["id"]
        ag1_auth = {"X-Agent-Key": a1.json()["agent_key"]}

        # Disable sharing
        await client.post("/v1/admin/trust/disable", headers=_admin())

        # Shared recall should return empty
        recall_resp = await client.post(
            f"/v1/agents/{agent1_id}/shared_views/recall",
            json={"query": "anything"},
            headers=ag1_auth,
        )
        assert recall_resp.status_code == 200
        data = recall_resp.json()
        assert data["atoms"] == []
        assert "disabled" in data.get("note", "").lower()

        # Re-enable
        await client.post("/v1/admin/trust/enable", headers=_admin())

    async def test_per_view_shared_recall_blocked_when_sharing_disabled(self, client, pool):
        """When sharing is disabled, per-view shared recall should return 403."""
        op_data = await _create_operator(
            client, username="pvrecall", email="pvrecall@test.com",
        )
        api_key = op_data["api_key"]
        op_auth = {"X-Operator-Key": api_key}

        a1 = await client.post(
            "/v1/agents",
            json={"name": "pvgranter", "domain_tags": ["test"]},
            headers=op_auth,
        )
        a2 = await client.post(
            "/v1/agents",
            json={"name": "pvgrantee", "domain_tags": ["test"]},
            headers=op_auth,
        )
        assert a1.status_code == 201 and a2.status_code == 201
        agent1_id = a1.json()["id"]
        agent2_id = a2.json()["id"]
        ag1_auth = {"X-Agent-Key": a1.json()["agent_key"]}
        ag2_auth = {"X-Agent-Key": a2.json()["agent_key"]}

        # Remember + view + enable sharing + grant
        await client.post(
            f"/v1/agents/{agent1_id}/remember",
            json={"text": "Per-view recall test memory."},
            headers=ag1_auth,
        )
        # Enable sharing for grant
        await client.post("/v1/admin/trust/enable", headers=_admin())

        view_resp = await client.post(
            f"/v1/agents/{agent1_id}/views",
            json={"name": "pvview", "atom_filter": {"domain_tags": ["test"]}},
            headers=ag1_auth,
        )
        assert view_resp.status_code == 201
        view_id = view_resp.json()["id"]

        grant_resp = await client.post(
            f"/v1/agents/{agent1_id}/grant",
            json={"view_id": view_id, "grantee_id": agent2_id},
            headers=ag1_auth,
        )
        assert grant_resp.status_code == 201

        # Now disable sharing
        await client.post("/v1/admin/trust/disable", headers=_admin())

        # Per-view shared recall should fail
        recall_resp = await client.post(
            f"/v1/agents/{agent2_id}/shared_views/{view_id}/recall",
            json={"query": "test"},
            headers=ag2_auth,
        )
        assert recall_resp.status_code == 403
        assert "disabled" in recall_resp.json()["detail"].lower()

        # Re-enable
        await client.post("/v1/admin/trust/enable", headers=_admin())
