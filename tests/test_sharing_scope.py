"""
Tests for per-operator sharing scope enforcement.

Covers:
- scope=none blocks all sharing operations
- scope=intra allows same-operator sharing, blocks cross-operator
- scope=full allows all sharing
- scope upgrade from none→intra enables sharing
"""

import pytest

from tests.conftest import TEST_ADMIN_KEY, admin_headers


def _admin():
    return admin_headers()


async def _create_operator(client, username, org="testorg", scope="none"):
    """Create operator and optionally set sharing scope."""
    resp = await client.post("/v1/admin/operators", headers=_admin(), json={
        "username": username, "org": org,
        "display_name": f"Op {username}", "email": f"{username}@test.com",
    })
    assert resp.status_code == 201, resp.text
    op_data = resp.json()

    if scope != "none":
        resp = await client.patch(
            f"/v1/admin/operators/{op_data['uuid']}/sharing-scope",
            headers=_admin(),
            json={"sharing_scope": scope},
        )
        assert resp.status_code == 200, resp.text

    return op_data


async def _create_agent(client, op_key, name="agent"):
    resp = await client.post(
        "/v1/agents", json={"name": name, "domain_tags": ["test"]},
        headers={"X-Operator-Key": op_key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_view_and_grant(client, agent_key, grantor_id, grantee_id):
    """Create a view, then grant it to grantee. Returns (view_id, capability_id)."""
    # Create a view
    resp = await client.post(
        f"/v1/agents/{grantor_id}/views",
        json={"name": "test-view", "atom_filter": {"domain_tags": ["test"]}},
        headers={"X-Agent-Key": agent_key},
    )
    assert resp.status_code == 201, resp.text
    view_id = resp.json()["id"]

    # Grant to grantee
    resp = await client.post(
        f"/v1/agents/{grantor_id}/grant",
        json={"view_id": view_id, "grantee_id": grantee_id, "permissions": ["read"]},
        headers={"X-Agent-Key": agent_key},
    )
    return resp, view_id


# ---------------------------------------------------------------------------
# scope=none tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestScopeNone:
    async def test_grant_returns_403(self, client):
        """scope=none: grant attempt returns 403."""
        op = await _create_operator(client, "scopenone1", scope="none")
        a1 = await _create_agent(client, op["api_key"], "alice")
        a2 = await _create_agent(client, op["api_key"], "bob")

        resp, _ = await _create_view_and_grant(
            client, a1["agent_key"], a1["id"], a2["id"],
        )
        assert resp.status_code == 403
        assert "not enabled" in resp.json()["detail"].lower()

    async def test_recall_shared_returns_403(self, client):
        """scope=none: recall_shared returns 403."""
        op = await _create_operator(client, "scopenone2", scope="none")
        a1 = await _create_agent(client, op["api_key"], "alice")

        resp = await client.post(
            f"/v1/agents/{a1['id']}/shared_views/recall",
            json={"query": "test"},
            headers={"X-Agent-Key": a1["agent_key"]},
        )
        assert resp.status_code == 403

    async def test_list_shared_returns_403(self, client):
        """scope=none: list_shared_views returns 403."""
        op = await _create_operator(client, "scopenone3", scope="none")
        a1 = await _create_agent(client, op["api_key"], "alice")

        resp = await client.get(
            f"/v1/agents/{a1['id']}/shared_views",
            headers={"X-Agent-Key": a1["agent_key"]},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# scope=intra tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestScopeIntra:
    async def test_same_operator_grant_succeeds(self, client):
        """scope=intra: sharing between agents of the same operator succeeds."""
        op = await _create_operator(client, "scopeintra1", scope="intra")
        a1 = await _create_agent(client, op["api_key"], "alice")
        a2 = await _create_agent(client, op["api_key"], "bob")

        resp, view_id = await _create_view_and_grant(
            client, a1["agent_key"], a1["id"], a2["id"],
        )
        assert resp.status_code == 201

    async def test_cross_operator_grant_blocked(self, client):
        """scope=intra: sharing with agent of different operator returns 403."""
        op_a = await _create_operator(client, "scopeintra2a", scope="intra")
        op_b = await _create_operator(client, "scopeintra2b", scope="intra")
        a1 = await _create_agent(client, op_a["api_key"], "alice")
        b1 = await _create_agent(client, op_b["api_key"], "charlie")

        resp, _ = await _create_view_and_grant(
            client, a1["agent_key"], a1["id"], b1["id"],
        )
        assert resp.status_code == 403
        assert "cross-operator" in resp.json()["detail"].lower()

    async def test_recall_shared_succeeds(self, client):
        """scope=intra: recall_shared works (scope != none)."""
        op = await _create_operator(client, "scopeintra3", scope="intra")
        a1 = await _create_agent(client, op["api_key"], "alice")

        resp = await client.post(
            f"/v1/agents/{a1['id']}/shared_views/recall",
            json={"query": "test"},
            headers={"X-Agent-Key": a1["agent_key"]},
        )
        # Should succeed (may return empty results, but not 403)
        assert resp.status_code == 200

    async def test_list_shared_succeeds(self, client):
        """scope=intra: list_shared_views works."""
        op = await _create_operator(client, "scopeintra4", scope="intra")
        a1 = await _create_agent(client, op["api_key"], "alice")

        resp = await client.get(
            f"/v1/agents/{a1['id']}/shared_views",
            headers={"X-Agent-Key": a1["agent_key"]},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# scope=full tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestScopeFull:
    async def test_cross_operator_grant_succeeds(self, client):
        """scope=full: cross-operator sharing is allowed."""
        op_a = await _create_operator(client, "scopefull1a", scope="full")
        op_b = await _create_operator(client, "scopefull1b", scope="full")
        a1 = await _create_agent(client, op_a["api_key"], "alice")
        b1 = await _create_agent(client, op_b["api_key"], "charlie")

        resp, _ = await _create_view_and_grant(
            client, a1["agent_key"], a1["id"], b1["id"],
        )
        assert resp.status_code == 201

    async def test_same_operator_grant_succeeds(self, client):
        """scope=full: same-operator sharing still works."""
        op = await _create_operator(client, "scopefull2", scope="full")
        a1 = await _create_agent(client, op["api_key"], "alice")
        a2 = await _create_agent(client, op["api_key"], "bob")

        resp, _ = await _create_view_and_grant(
            client, a1["agent_key"], a1["id"], a2["id"],
        )
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Scope upgrade test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestScopeUpgrade:
    async def test_upgrade_none_to_intra(self, client):
        """Upgrading from none→intra enables sharing."""
        op = await _create_operator(client, "scopeup1", scope="none")
        a1 = await _create_agent(client, op["api_key"], "alice")
        a2 = await _create_agent(client, op["api_key"], "bob")

        # Attempt grant with scope=none → 403
        resp, _ = await _create_view_and_grant(
            client, a1["agent_key"], a1["id"], a2["id"],
        )
        assert resp.status_code == 403

        # Upgrade to intra
        resp = await client.patch(
            f"/v1/admin/operators/{op['uuid']}/sharing-scope",
            headers=_admin(),
            json={"sharing_scope": "intra"},
        )
        assert resp.status_code == 200
        assert resp.json()["sharing_scope"] == "intra"

        # Now grant succeeds
        resp, _ = await _create_view_and_grant(
            client, a1["agent_key"], a1["id"], a2["id"],
        )
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Admin endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAdminSharingScope:
    async def test_set_scope_valid(self, client):
        op = await _create_operator(client, "scopeadm1")
        for scope in ("intra", "full", "none"):
            resp = await client.patch(
                f"/v1/admin/operators/{op['uuid']}/sharing-scope",
                headers=_admin(),
                json={"sharing_scope": scope},
            )
            assert resp.status_code == 200
            assert resp.json()["sharing_scope"] == scope

    async def test_set_scope_invalid(self, client):
        op = await _create_operator(client, "scopeadm2")
        resp = await client.patch(
            f"/v1/admin/operators/{op['uuid']}/sharing-scope",
            headers=_admin(),
            json={"sharing_scope": "bogus"},
        )
        assert resp.status_code == 422

    async def test_scope_in_operator_list(self, client):
        await _create_operator(client, "scopeadm3", scope="intra")
        resp = await client.get("/v1/admin/operators", headers=_admin())
        assert resp.status_code == 200
        ops = resp.json()["operators"]
        found = [o for o in ops if o["username"] == "scopeadm3"]
        assert len(found) == 1
        assert found[0]["sharing_scope"] == "intra"

    async def test_scope_in_operator_show(self, client):
        op = await _create_operator(client, "scopeadm4", scope="full")
        resp = await client.get(f"/v1/admin/operators/{op['uuid']}", headers=_admin())
        assert resp.status_code == 200
        assert resp.json()["sharing_scope"] == "full"

    async def test_scope_in_auth_me(self, client):
        op = await _create_operator(client, "scopeadm5", scope="intra")
        resp = await client.get(
            "/v1/auth/me", headers={"X-Operator-Key": op["api_key"]},
        )
        assert resp.status_code == 200
        assert resp.json()["sharing_scope"] == "intra"

    async def test_set_scope_not_found(self, client):
        import uuid
        resp = await client.patch(
            f"/v1/admin/operators/{uuid.uuid4()}/sharing-scope",
            headers=_admin(),
            json={"sharing_scope": "intra"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Revoke always works regardless of scope
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRevokeIgnoresScope:
    async def test_revoke_works_with_scope_none(self, client):
        """Revoking capabilities should work even when scope=none."""
        # Create with intra, grant, then downgrade to none, then revoke
        op = await _create_operator(client, "scoperev1", scope="intra")
        a1 = await _create_agent(client, op["api_key"], "alice")
        a2 = await _create_agent(client, op["api_key"], "bob")

        resp, view_id = await _create_view_and_grant(
            client, a1["agent_key"], a1["id"], a2["id"],
        )
        assert resp.status_code == 201
        cap_id = resp.json()["id"]

        # Downgrade to none
        await client.patch(
            f"/v1/admin/operators/{op['uuid']}/sharing-scope",
            headers=_admin(),
            json={"sharing_scope": "none"},
        )

        # Revoke should still work
        resp = await client.post(
            f"/v1/capabilities/{cap_id}/revoke",
            headers={"X-Agent-Key": a1["agent_key"]},
        )
        assert resp.status_code == 200
        assert resp.json()["revoked"] is True
