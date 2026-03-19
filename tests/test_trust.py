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

    async def test_same_org_agents_trust_each_other(self, client, pool, clean_db):
        """Agents created under the same org should auto-trust bidirectionally."""
        r1 = await client.post("/v1/agents", json={"name": "alpha", "domain_tags": ["test"]})
        r2 = await client.post("/v1/agents", json={"name": "beta", "domain_tags": ["test"]})
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

    async def test_third_agent_trusts_both_existing(self, client, pool, clean_db):
        """A third agent should auto-trust both existing same-org agents."""
        r1 = await client.post("/v1/agents", json={"name": "a1", "domain_tags": []})
        r2 = await client.post("/v1/agents", json={"name": "a2", "domain_tags": []})
        r3 = await client.post("/v1/agents", json={"name": "a3", "domain_tags": []})
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

    async def test_single_agent_has_no_trust_rows(self, client, pool, clean_db):
        """A lone agent in an org has no trust rows (no one to trust)."""
        r = await client.post("/v1/agents", json={"name": "solo", "domain_tags": []})
        agent_id = UUID(r.json()["id"])

        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM agent_trust WHERE agent_uuid = $1 OR trusted_sender_uuid = $1",
                agent_id,
            )
            assert count == 0


# ── Trust gating on recall ────────────────────────────────────────────────────

class TestTrustGating:

    async def _setup_shared_view(self, client, pool):
        """Create two agents, store atoms on alice, create view, grant to bob."""
        r1 = await client.post("/v1/agents", json={"name": "alice", "domain_tags": ["shared"]})
        r2 = await client.post("/v1/agents", json={"name": "bob", "domain_tags": ["shared"]})
        alice = r1.json()
        bob = r2.json()

        await remember(client, alice["id"], "The deployment pipeline uses blue-green strategy.", domain_tags=["devops"])

        # Create view
        view_resp = await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "devops-knowledge",
            "atom_filter": {"domain_tags": ["devops"]},
        })
        assert view_resp.status_code == 201
        view = view_resp.json()

        # Grant to bob
        grant_resp = await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": bob["id"],
        })
        assert grant_resp.status_code == 201

        return alice, bob, view

    async def test_trusted_recall_shared_returns_atoms(self, client, pool, clean_db):
        """recall_shared returns atoms when trust exists (auto-seeded same-org)."""
        alice, bob, view = await self._setup_shared_view(client, pool)

        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "deployment strategy"},
        )
        assert resp.status_code == 200
        assert resp.json()["total_retrieved"] >= 1

    async def test_untrusted_recall_shared_returns_empty(self, client, pool, clean_db):
        """recall_shared returns nothing when trust is removed."""
        alice, bob, view = await self._setup_shared_view(client, pool)

        # Remove trust: bob no longer trusts alice
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_trust WHERE agent_uuid = $1 AND trusted_sender_uuid = $2",
                UUID(bob["id"]), UUID(alice["id"]),
            )

        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "deployment strategy"},
        )
        assert resp.status_code == 200
        assert resp.json()["total_retrieved"] == 0

    async def test_untrusted_recall_all_shared_returns_empty(self, client, pool, clean_db):
        """recall_all_shared filters out atoms from untrusted grantors."""
        alice, bob, view = await self._setup_shared_view(client, pool)

        # Remove trust
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_trust WHERE agent_uuid = $1 AND trusted_sender_uuid = $2",
                UUID(bob["id"]), UUID(alice["id"]),
            )

        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/recall",
            json={"query": "deployment strategy"},
        )
        assert resp.status_code == 200
        assert resp.json()["total_retrieved"] == 0

    async def test_trusted_recall_all_shared_returns_atoms(self, client, pool, clean_db):
        """recall_all_shared returns atoms when trust exists."""
        alice, bob, view = await self._setup_shared_view(client, pool)

        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/recall",
            json={"query": "deployment strategy"},
        )
        assert resp.status_code == 200
        assert resp.json()["total_retrieved"] >= 1

    async def test_restoring_trust_makes_atoms_visible_again(self, client, pool, clean_db):
        """Removing then re-adding trust should restore access."""
        alice, bob, view = await self._setup_shared_view(client, pool)
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
        )
        assert resp.json()["total_retrieved"] >= 1


# ── list_shared_views trusted field ───────────────────────────────────────────

class TestListSharedViewsTrust:

    async def test_trusted_field_true_for_trusted_grantor(self, client, pool, clean_db):
        """Shared views from trusted grantors should have trusted=True."""
        r1 = await client.post("/v1/agents", json={"name": "alice", "domain_tags": []})
        r2 = await client.post("/v1/agents", json={"name": "bob", "domain_tags": []})
        alice, bob = r1.json(), r2.json()

        await remember(client, alice["id"], "Test fact for view.", domain_tags=["test"])
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "test-view", "atom_filter": {},
        })).json()

        await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"], "grantee_id": bob["id"],
        })

        resp = await client.get(f"/v1/agents/{bob['id']}/shared_views")
        assert resp.status_code == 200
        views = resp.json()
        assert len(views) >= 1
        assert views[0]["trusted"] is True

    async def test_trusted_field_false_for_untrusted_grantor(self, client, pool, clean_db):
        """Shared views from untrusted grantors should have trusted=False."""
        r1 = await client.post("/v1/agents", json={"name": "alice", "domain_tags": []})
        r2 = await client.post("/v1/agents", json={"name": "bob", "domain_tags": []})
        alice, bob = r1.json(), r2.json()

        await remember(client, alice["id"], "Test fact for view.", domain_tags=["test"])
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "test-view", "atom_filter": {},
        })).json()

        await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"], "grantee_id": bob["id"],
        })

        # Remove trust
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM agent_trust WHERE agent_uuid = $1 AND trusted_sender_uuid = $2",
                UUID(bob["id"]), UUID(alice["id"]),
            )

        resp = await client.get(f"/v1/agents/{bob['id']}/shared_views")
        views = resp.json()
        assert len(views) >= 1
        assert views[0]["trusted"] is False
