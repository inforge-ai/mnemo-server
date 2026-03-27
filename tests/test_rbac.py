"""
RBAC-Lite (Tier 2) tests.

Tests the core permission enforcement:
- Agent key A cannot access Agent B's endpoints
- Operator key cannot call agent-level endpoints
- Agent key cannot call operator-level endpoints
- Mismatched agent_id in URL vs key → 403
- Admin key can do everything
- Wrong/missing key → 401
"""

import pytest

from tests.conftest import TEST_ADMIN_KEY, admin_headers, remember


@pytest.mark.asyncio
class TestRBACKeyTypes:
    """Test that each key type can only access its allowed endpoints."""

    async def test_agent_key_can_remember(self, client, agent_with_key):
        """Agent key should be able to call /remember."""
        agent_data, _, ag_headers = agent_with_key
        agent_id = agent_data["id"]
        resp = await client.post(
            f"/v1/agents/{agent_id}/remember",
            json={"text": "Test memory from RBAC test"},
            headers=ag_headers,
        )
        assert resp.status_code == 201

    async def test_agent_key_can_recall(self, client, agent_with_key):
        """Agent key should be able to call /recall."""
        agent_data, _, ag_headers = agent_with_key
        agent_id = agent_data["id"]
        await remember(client, agent_id, "something to recall", headers=ag_headers)
        resp = await client.post(
            f"/v1/agents/{agent_id}/recall",
            json={"query": "something"},
            headers=ag_headers,
        )
        assert resp.status_code == 200

    async def test_agent_key_can_get_stats(self, client, agent_with_key):
        """Agent key should be able to call /stats."""
        agent_data, _, ag_headers = agent_with_key
        resp = await client.get(
            f"/v1/agents/{agent_data['id']}/stats",
            headers=ag_headers,
        )
        assert resp.status_code == 200

    async def test_operator_key_cannot_remember(self, client, agent_with_key, operator_with_key):
        """Operator key should NOT be able to call /remember (agent-level)."""
        agent_data, _, _ = agent_with_key
        _, _, op_headers = operator_with_key
        resp = await client.post(
            f"/v1/agents/{agent_data['id']}/remember",
            json={"text": "should fail"},
            headers=op_headers,
        )
        assert resp.status_code == 403

    async def test_operator_key_cannot_recall(self, client, agent_with_key, operator_with_key):
        """Operator key should NOT be able to call /recall (agent-level)."""
        agent_data, _, _ = agent_with_key
        _, _, op_headers = operator_with_key
        resp = await client.post(
            f"/v1/agents/{agent_data['id']}/recall",
            json={"query": "should fail"},
            headers=op_headers,
        )
        assert resp.status_code == 403

    async def test_agent_key_cannot_register_agent(self, client, agent_with_key):
        """Agent key should NOT be able to register new agents (operator-level)."""
        _, _, ag_headers = agent_with_key
        resp = await client.post(
            "/v1/agents",
            json={"name": "should-fail", "domain_tags": []},
            headers=ag_headers,
        )
        assert resp.status_code == 403

    async def test_agent_key_cannot_list_agents(self, client, agent_with_key):
        """Agent key should NOT be able to list agents (operator-level)."""
        _, _, ag_headers = agent_with_key
        resp = await client.get("/v1/agents", headers=ag_headers)
        assert resp.status_code == 403

    async def test_admin_key_can_do_everything(self, client, agent_with_key):
        """Admin key should be able to call any endpoint."""
        agent_data, _, _ = agent_with_key
        agent_id = agent_data["id"]
        h = admin_headers()

        # Agent-level endpoints
        resp = await client.post(
            f"/v1/agents/{agent_id}/remember",
            json={"text": "admin test memory"},
            headers=h,
        )
        assert resp.status_code == 201

        resp = await client.post(
            f"/v1/agents/{agent_id}/recall",
            json={"query": "admin test"},
            headers=h,
        )
        assert resp.status_code == 200

        # Operator-level endpoints
        resp = await client.get("/v1/agents", headers=h)
        assert resp.status_code == 200

        # Admin-level endpoints
        resp = await client.get("/v1/admin/agents", headers=h)
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestRBACAgentIsolation:
    """Test that Agent A cannot access Agent B's resources."""

    async def test_agent_a_cannot_remember_as_agent_b(self, client, operator_with_key):
        """Agent A's key cannot hit Agent B's /remember endpoint."""
        _, _, op_headers = operator_with_key

        # Create two agents
        r1 = await client.post("/v1/agents", json={"name": "agent-a", "domain_tags": []}, headers=op_headers)
        r2 = await client.post("/v1/agents", json={"name": "agent-b", "domain_tags": []}, headers=op_headers)
        assert r1.status_code == 201 and r2.status_code == 201

        agent_a_key = r1.json()["agent_key"]
        agent_b_id = r2.json()["id"]

        # Agent A tries to remember on Agent B's endpoint
        resp = await client.post(
            f"/v1/agents/{agent_b_id}/remember",
            json={"text": "injected memory"},
            headers={"X-Agent-Key": agent_a_key},
        )
        assert resp.status_code == 403
        assert "does not match" in resp.json()["detail"]

    async def test_agent_a_cannot_recall_as_agent_b(self, client, operator_with_key):
        """Agent A's key cannot hit Agent B's /recall endpoint."""
        _, _, op_headers = operator_with_key

        r1 = await client.post("/v1/agents", json={"name": "agent-a", "domain_tags": []}, headers=op_headers)
        r2 = await client.post("/v1/agents", json={"name": "agent-b", "domain_tags": []}, headers=op_headers)

        agent_a_key = r1.json()["agent_key"]
        agent_b_id = r2.json()["id"]

        resp = await client.post(
            f"/v1/agents/{agent_b_id}/recall",
            json={"query": "stolen data"},
            headers={"X-Agent-Key": agent_a_key},
        )
        assert resp.status_code == 403

    async def test_agent_a_cannot_get_agent_b_stats(self, client, operator_with_key):
        """Agent A's key cannot hit Agent B's /stats endpoint."""
        _, _, op_headers = operator_with_key

        r1 = await client.post("/v1/agents", json={"name": "agent-a", "domain_tags": []}, headers=op_headers)
        r2 = await client.post("/v1/agents", json={"name": "agent-b", "domain_tags": []}, headers=op_headers)

        agent_a_key = r1.json()["agent_key"]
        agent_b_id = r2.json()["id"]

        resp = await client.get(
            f"/v1/agents/{agent_b_id}/stats",
            headers={"X-Agent-Key": agent_a_key},
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
class TestRBACMissingCredentials:
    """Test 401 for missing or invalid credentials."""

    async def test_no_credentials_returns_401(self, client):
        resp = await client.post("/v1/agents", json={"name": "test", "domain_tags": []})
        assert resp.status_code == 401

    async def test_invalid_agent_key_returns_401(self, client):
        resp = await client.post(
            "/v1/agents/00000000-0000-0000-0000-000000000000/remember",
            json={"text": "test"},
            headers={"X-Agent-Key": "mnemo_ag_invalid_key_here"},
        )
        assert resp.status_code == 401

    async def test_invalid_operator_key_returns_401(self, client):
        resp = await client.post(
            "/v1/agents",
            json={"name": "test", "domain_tags": []},
            headers={"X-Operator-Key": "mnemo_op_invalid_key_here"},
        )
        assert resp.status_code == 401

    async def test_invalid_admin_key_returns_401(self, client):
        resp = await client.get(
            "/v1/admin/agents",
            headers={"X-Admin-Key": "wrong_admin_key"},
        )
        assert resp.status_code == 401
