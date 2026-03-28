"""Tests for the agent trust layer (sharing auth).

Verifies:
- Auto-seeding of trust rows on agent creation (same-org)
- recall_shared and recall_all_shared filter by trust
- list_shared_views includes trusted field
- Trust CRUD (add/remove)
- Untrusted shares are invisible but not deleted
"""

import pytest
from uuid import UUID
from tests.conftest import remember


# ── Auto-seeding ──────────────────────────────────────────────────────────────

class TestTrustAutoSeed:

    async def test_same_org_agents_trust_each_other(self, client, pool, operator_with_key):
        """Agents created under the same org should auto-trust bidirectionally."""
        _, _, op_headers = operator_with_key
        r1 = await client.post("/v1/agents", json={"name": "alpha", "domain_tags": ["test"]}, headers=op_headers)
        r2 = await client.post("/v1/agents", json={"name": "beta", "domain_tags": ["test"]}, headers=op_headers)
        assert r1.status_code == 201
        assert r2.status_code == 201
        a1 = UUID(r1.json()["id"])
        a2 = UUID(r2.json()["id"])

        async with pool.acquire() as conn:
            # alpha trusts beta
            row = await conn.fetchrow(
                "SELECT 1 FROM agent_trust WHERE agent_uuid = $1 AND trusted_sender_uuid = $2",
                a1, a2,
            )
            assert row is not None, "alpha should trust beta"

            # beta trusts alpha
            row = await conn.fetchrow(
                "SELECT 1 FROM agent_trust WHERE agent_uuid = $1 AND trusted_sender_uuid = $2",
                a2, a1,
            )
            assert row is not None, "beta should trust alpha"

    async def test_third_agent_trusts_both_existing(self, client, pool, operator_with_key):
        """A third agent should auto-trust both existing same-org agents."""
        _, _, op_headers = operator_with_key
        r1 = await client.post("/v1/agents", json={"name": "a1", "domain_tags": []}, headers=op_headers)
        r2 = await client.post("/v1/agents", json={"name": "a2", "domain_tags": []}, headers=op_headers)
        r3 = await client.post("/v1/agents", json={"name": "a3", "domain_tags": []}, headers=op_headers)
        a1 = UUID(r1.json()["id"])
        a2 = UUID(r2.json()["id"])
        a3 = UUID(r3.json()["id"])

        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_trust WHERE agent_uuid = $1", a3,
            )
            assert count == 2, "a3 should trust a1 and a2"

            count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_trust WHERE trusted_sender_uuid = $1", a3,
            )
            assert count == 2, "a1 and a2 should both trust a3"

    async def test_single_agent_has_no_trust_rows(self, client, pool, operator_with_key):
        """A lone agent in an org has no trust rows (no one to trust)."""
        _, _, op_headers = operator_with_key
        r = await client.post("/v1/agents", json={"name": "solo", "domain_tags": []}, headers=op_headers)
        agent_id = UUID(r.json()["id"])

        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_trust WHERE agent_uuid = $1 OR trusted_sender_uuid = $1",
                agent_id,
            )
            assert count == 0


# ── Trust gating on recall ────────────────────────────────────────────────────

class TestTrustGating:

    async def _setup_shared_view(self, client, pool, op_headers):
        """Create two agents, store atoms on alice, create view, grant to bob."""
        r1 = await client.post("/v1/agents", json={"name": "alice", "domain_tags": ["shared"]}, headers=op_headers)
        r2 = await client.post("/v1/agents", json={"name": "bob", "domain_tags": ["shared"]}, headers=op_headers)
        alice = r1.json()
        bob = r2.json()
        alice_headers = {"X-Agent-Key": alice["agent_key"]}
        bob_headers = {"X-Agent-Key": bob["agent_key"]}

        await remember(client, alice["id"], "The deployment pipeline uses blue-green strategy.", domain_tags=["devops"], headers=alice_headers)

        # Create view
        view_resp = await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "devops-knowledge",
            "atom_filter": {"domain_tags": ["devops"]},
        }, headers=alice_headers)
        assert view_resp.status_code == 201
        view = view_resp.json()

        # Grant to bob
        grant_resp = await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": bob["id"],
        }, headers=alice_headers)
        assert grant_resp.status_code == 201

        return alice, bob, view, alice_headers, bob_headers

    async def test_trusted_recall_shared_returns_atoms(self, client, pool, operator_with_key):
        """recall_shared returns atoms when trust exists (auto-seeded same-org)."""
        _, _, op_headers = operator_with_key
        alice, bob, view, alice_headers, bob_headers = await self._setup_shared_view(client, pool, op_headers)

        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "deployment strategy"},
            headers=bob_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["total_retrieved"] >= 1

    async def test_untrusted_recall_shared_returns_empty(self, client, pool, operator_with_key):
        """recall_shared returns nothing when trust is removed."""
        _, _, op_headers = operator_with_key
        alice, bob, view, alice_headers, bob_headers = await self._setup_shared_view(client, pool, op_headers)

        # Remove trust: bob no longer trusts alice
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_trust WHERE agent_uuid = $1 AND trusted_sender_uuid = $2",
                UUID(bob["id"]), UUID(alice["id"]),
            )

        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "deployment strategy"},
            headers=bob_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["total_retrieved"] == 0

    async def test_untrusted_recall_all_shared_returns_empty(self, client, pool, operator_with_key):
        """recall_all_shared filters out atoms from untrusted grantors."""
        _, _, op_headers = operator_with_key
        alice, bob, view, alice_headers, bob_headers = await self._setup_shared_view(client, pool, op_headers)

        # Remove trust
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_trust WHERE agent_uuid = $1 AND trusted_sender_uuid = $2",
                UUID(bob["id"]), UUID(alice["id"]),
            )

        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/recall",
            json={"query": "deployment strategy"},
            headers=bob_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["total_retrieved"] == 0

    async def test_trusted_recall_all_shared_returns_atoms(self, client, pool, operator_with_key):
        """recall_all_shared returns atoms when trust exists."""
        _, _, op_headers = operator_with_key
        alice, bob, view, alice_headers, bob_headers = await self._setup_shared_view(client, pool, op_headers)

        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/recall",
            json={"query": "deployment strategy"},
            headers=bob_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["total_retrieved"] >= 1

    async def test_restoring_trust_makes_atoms_visible_again(self, client, pool, operator_with_key):
        """Removing then re-adding trust should restore access."""
        _, _, op_headers = operator_with_key
        alice, bob, view, alice_headers, bob_headers = await self._setup_shared_view(client, pool, op_headers)
        alice_id, bob_id = UUID(alice["id"]), UUID(bob["id"])

        # Remove trust
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_trust WHERE agent_uuid = $1 AND trusted_sender_uuid = $2",
                bob_id, alice_id,
            )

        # Verify empty
        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "deployment"},
            headers=bob_headers,
        )
        assert resp.json()["total_retrieved"] == 0

        # Re-add trust
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO agent_trust (agent_uuid, trusted_sender_uuid) VALUES ($1, $2)",
                bob_id, alice_id,
            )

        # Should see atoms again
        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "deployment"},
            headers=bob_headers,
        )
        assert resp.json()["total_retrieved"] >= 1


# ── list_shared_views trusted field ───────────────────────────────────────────

class TestListSharedViewsTrust:

    async def test_trusted_field_true_for_trusted_grantor(self, client, pool, operator_with_key):
        """Shared views from trusted grantors should have trusted=True."""
        _, _, op_headers = operator_with_key
        r1 = await client.post("/v1/agents", json={"name": "alice", "domain_tags": []}, headers=op_headers)
        r2 = await client.post("/v1/agents", json={"name": "bob", "domain_tags": []}, headers=op_headers)
        alice, bob = r1.json(), r2.json()
        alice_headers = {"X-Agent-Key": alice["agent_key"]}
        bob_headers = {"X-Agent-Key": bob["agent_key"]}

        await remember(client, alice["id"], "Test fact for view.", domain_tags=["test"], headers=alice_headers)
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "test-view", "atom_filter": {},
        }, headers=alice_headers)).json()

        await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"], "grantee_id": bob["id"],
        }, headers=alice_headers)

        resp = await client.get(f"/v1/agents/{bob['id']}/shared_views", headers=bob_headers)
        assert resp.status_code == 200
        views = resp.json()
        assert len(views) >= 1
        assert views[0]["trusted"] is True

    async def test_trusted_field_false_for_untrusted_grantor(self, client, pool, operator_with_key):
        """Shared views from untrusted grantors should have trusted=False."""
        _, _, op_headers = operator_with_key
        r1 = await client.post("/v1/agents", json={"name": "alice", "domain_tags": []}, headers=op_headers)
        r2 = await client.post("/v1/agents", json={"name": "bob", "domain_tags": []}, headers=op_headers)
        alice, bob = r1.json(), r2.json()
        alice_headers = {"X-Agent-Key": alice["agent_key"]}
        bob_headers = {"X-Agent-Key": bob["agent_key"]}

        await remember(client, alice["id"], "Test fact for view.", domain_tags=["test"], headers=alice_headers)
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "test-view", "atom_filter": {},
        }, headers=alice_headers)).json()

        await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"], "grantee_id": bob["id"],
        }, headers=alice_headers)

        # Remove trust
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_trust WHERE agent_uuid = $1 AND trusted_sender_uuid = $2",
                UUID(bob["id"]), UUID(alice["id"]),
            )

        resp = await client.get(f"/v1/agents/{bob['id']}/shared_views", headers=bob_headers)
        views = resp.json()
        assert len(views) >= 1
        assert views[0]["trusted"] is False
