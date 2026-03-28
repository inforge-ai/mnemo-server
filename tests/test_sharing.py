"""Tests for sharing features: query-based views, cross-view recall."""

import pytest
import pytest_asyncio
from tests.conftest import remember


@pytest.mark.asyncio
class TestQueryBasedViewCreation:
    async def test_query_selects_relevant_atoms(self, client, agent):
        """View created with query= should only include semantically relevant atoms."""
        headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], "Always validate SQL parameters to prevent injection attacks.", domain_tags=["security"], headers=headers)
        await remember(client, agent["id"], "The cafeteria serves good pasta on Tuesdays.", domain_tags=["food"], headers=headers)
        await remember(client, agent["id"], "Use prepared statements for database queries.", domain_tags=["security"], headers=headers)

        resp = await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "sql-security",
            "atom_filter": {
                "query": "SQL injection prevention and database security",
                "max_atoms": 5,
            },
        }, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["atom_count"] >= 1
        assert data["atom_count"] <= 5

    async def test_view_without_query_snapshots_all(self, client, agent):
        """View without query= still snapshots ALL matching atoms."""
        headers = {"X-Agent-Key": agent["agent_key"]}
        texts = [
            "Python decorators wrap functions to add behavior transparently.",
            "The GIL prevents true parallelism in CPython threads.",
            "List comprehensions are faster than equivalent for-loops in Python.",
        ]
        for text in texts:
            await remember(client, agent["id"], text, domain_tags=["python"], headers=headers)

        resp = await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "all-python",
            "atom_filter": {"domain_tags": ["python"]},
        }, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["atom_count"] >= 3


@pytest.mark.asyncio
class TestSharedViewsEnrichment:
    async def test_shared_views_include_grantor_address(self, client, pool, operator_with_username):
        """list_shared_views response includes source_address and granted_at."""
        op = operator_with_username
        async with pool.acquire() as conn:
            alice = await conn.fetchrow("""
                INSERT INTO agents (operator_id, name, domain_tags)
                VALUES ($1, 'alice', '{}') RETURNING id
            """, op["id"])
            bob = await conn.fetchrow("""
                INSERT INTO agents (operator_id, name, domain_tags)
                VALUES ($1, 'bob', '{}') RETURNING id
            """, op["id"])

            from mnemo.server.services.address_service import create_address
            from mnemo.server.services.auth_service import create_agent_key
            await create_address(conn, alice["id"], "alice", op["username"], op["org"])
            await create_address(conn, bob["id"], "bob", op["username"], op["org"])
            alice_key = await create_agent_key(conn, alice["id"])
            bob_key = await create_agent_key(conn, bob["id"])

        alice_headers = {"X-Agent-Key": alice_key}
        bob_headers = {"X-Agent-Key": bob_key}

        view_resp = await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "alice-knowledge", "atom_filter": {},
        }, headers=alice_headers)
        assert view_resp.status_code == 201
        view = view_resp.json()

        grant_resp = await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": str(bob["id"]),
        }, headers=alice_headers)
        assert grant_resp.status_code == 201

        shared_resp = await client.get(f"/v1/agents/{bob['id']}/shared_views", headers=bob_headers)
        assert shared_resp.status_code == 200
        shared = shared_resp.json()
        assert len(shared) == 1
        assert shared[0]["source_address"] == f"alice:{op['username']}.{op['org']}"
        assert "granted_at" in shared[0]
        assert shared[0]["grantor_id"] == str(alice["id"])


@pytest.mark.asyncio
class TestCrossViewRecall:
    async def _setup_sharing(self, client, pool, operator_with_username):
        """Helper: create alice with memories, share with bob."""
        op = operator_with_username
        async with pool.acquire() as conn:
            alice = await conn.fetchrow("""
                INSERT INTO agents (operator_id, name, domain_tags)
                VALUES ($1, 'alice', '{}') RETURNING id
            """, op["id"])
            bob = await conn.fetchrow("""
                INSERT INTO agents (operator_id, name, domain_tags)
                VALUES ($1, 'bob', '{}') RETURNING id
            """, op["id"])
            from mnemo.server.services.address_service import create_address
            from mnemo.server.services.auth_service import create_agent_key
            await create_address(conn, alice["id"], "alice", op["username"], op["org"])
            await create_address(conn, bob["id"], "bob", op["username"], op["org"])
            alice_key = await create_agent_key(conn, alice["id"])
            bob_key = await create_agent_key(conn, bob["id"])
            # Create trust rows so shared recall works (bob trusts alice)
            await conn.execute(
                "INSERT INTO agent_trust (agent_uuid, trusted_sender_uuid) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                bob["id"], alice["id"],
            )

        alice_headers = {"X-Agent-Key": alice_key}
        bob_headers = {"X-Agent-Key": bob_key}

        # Alice stores memories
        await remember(client, str(alice["id"]), "Always check NII sustainability against rate expectations for bank earnings.", domain_tags=["finance"], headers=alice_headers)
        await remember(client, str(alice["id"]), "Revenue growth in tech sector correlates with R&D spending.", domain_tags=["finance"], headers=alice_headers)

        # Alice creates a view and grants to Bob
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "finance-knowledge",
            "atom_filter": {"domain_tags": ["finance"]},
        }, headers=alice_headers)).json()

        await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": str(bob["id"]),
        }, headers=alice_headers)

        return {
            "alice": dict(alice), "bob": dict(bob), "view": view, "op": op,
            "alice_key": alice_key, "bob_key": bob_key,
            "alice_headers": alice_headers, "bob_headers": bob_headers,
        }

    async def test_cross_view_recall_returns_results(self, client, pool, operator_with_username):
        ctx = await self._setup_sharing(client, pool, operator_with_username)
        bob = ctx["bob"]

        # Use a query that closely matches the stored text for reliable similarity
        resp = await client.post(f"/v1/agents/{bob['id']}/shared_views/recall", json={
            "query": "NII sustainability rate expectations bank earnings",
            "min_similarity": 0.2,
        }, headers=ctx["bob_headers"])
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["atoms"]) >= 1
        for atom in data["atoms"]:
            assert "source_address" in atom
            assert "view_name" in atom

    async def test_cross_view_recall_scope_safety(self, client, pool, operator_with_username):
        """Atoms NOT in the shared view should not appear."""
        ctx = await self._setup_sharing(client, pool, operator_with_username)
        alice, bob = ctx["alice"], ctx["bob"]

        # Alice stores a memory AFTER creating the view
        await remember(client, str(alice["id"]), "Secret proprietary trading strategy that should not be shared.", domain_tags=["finance"], headers=ctx["alice_headers"])

        resp = await client.post(f"/v1/agents/{bob['id']}/shared_views/recall", json={
            "query": "proprietary trading strategy",
        }, headers=ctx["bob_headers"])
        assert resp.status_code == 200
        data = resp.json()
        for atom in data["atoms"]:
            assert "proprietary" not in atom["text_content"].lower()

    async def test_cross_view_recall_no_shared_views(self, client, agent):
        """Agent with no shared views gets empty result."""
        headers = {"X-Agent-Key": agent["agent_key"]}
        resp = await client.post(f"/v1/agents/{agent['id']}/shared_views/recall", json={
            "query": "anything",
        }, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["atoms"] == []
        assert data["total_retrieved"] == 0
