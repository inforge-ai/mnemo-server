"""Tests for sharing features: query-based views, cross-view recall."""

import pytest
import pytest_asyncio
from tests.conftest import remember


@pytest.mark.asyncio
class TestQueryBasedViewCreation:
    async def test_query_selects_relevant_atoms(self, client, agent):
        """View created with query= should only include semantically relevant atoms."""
        await remember(client, agent["id"], "Always validate SQL parameters to prevent injection attacks.", domain_tags=["security"])
        await remember(client, agent["id"], "The cafeteria serves good pasta on Tuesdays.", domain_tags=["food"])
        await remember(client, agent["id"], "Use prepared statements for database queries.", domain_tags=["security"])

        resp = await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "sql-security",
            "atom_filter": {
                "query": "SQL injection prevention and database security",
                "max_atoms": 5,
            },
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["atom_count"] >= 1
        assert data["atom_count"] <= 5

    async def test_view_without_query_snapshots_all(self, client, agent):
        """View without query= still snapshots ALL matching atoms."""
        texts = [
            "Python decorators wrap functions to add behavior transparently.",
            "The GIL prevents true parallelism in CPython threads.",
            "List comprehensions are faster than equivalent for-loops in Python.",
        ]
        for text in texts:
            await remember(client, agent["id"], text, domain_tags=["python"])

        resp = await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "all-python",
            "atom_filter": {"domain_tags": ["python"]},
        })
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
            await create_address(conn, alice["id"], "alice", op["username"], op["org"])
            await create_address(conn, bob["id"], "bob", op["username"], op["org"])

        view_resp = await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "alice-knowledge", "atom_filter": {},
        })
        assert view_resp.status_code == 201
        view = view_resp.json()

        grant_resp = await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": str(bob["id"]),
        })
        assert grant_resp.status_code == 201

        shared_resp = await client.get(f"/v1/agents/{bob['id']}/shared_views")
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
            await create_address(conn, alice["id"], "alice", op["username"], op["org"])
            await create_address(conn, bob["id"], "bob", op["username"], op["org"])

        # Alice stores memories
        await remember(client, str(alice["id"]), "Always check NII sustainability against rate expectations for bank earnings.", domain_tags=["finance"])
        await remember(client, str(alice["id"]), "Revenue growth in tech sector correlates with R&D spending.", domain_tags=["finance"])

        # Alice creates a view and grants to Bob
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "finance-knowledge",
            "atom_filter": {"domain_tags": ["finance"]},
        })).json()

        await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": str(bob["id"]),
        })

        return {"alice": dict(alice), "bob": dict(bob), "view": view, "op": op}

    async def test_cross_view_recall_returns_results(self, client, pool, operator_with_username):
        ctx = await self._setup_sharing(client, pool, operator_with_username)
        bob = ctx["bob"]

        resp = await client.post(f"/v1/agents/{bob['id']}/shared_views/recall", json={
            "query": "bank earnings analysis",
        })
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
        await remember(client, str(alice["id"]), "Secret proprietary trading strategy that should not be shared.", domain_tags=["finance"])

        resp = await client.post(f"/v1/agents/{bob['id']}/shared_views/recall", json={
            "query": "proprietary trading strategy",
        })
        assert resp.status_code == 200
        data = resp.json()
        for atom in data["atoms"]:
            assert "proprietary" not in atom["text_content"].lower()

    async def test_cross_view_recall_no_shared_views(self, client, agent):
        """Agent with no shared views gets empty result."""
        resp = await client.post(f"/v1/agents/{agent['id']}/shared_views/recall", json={
            "query": "anything",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["atoms"] == []
        assert data["total_retrieved"] == 0
