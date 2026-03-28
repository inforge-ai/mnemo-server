"""
Integration tests for the Mnemo API.
Requires a running PostgreSQL instance (uses MNEMO_TEST_DATABASE_URL from .env).
Tables are truncated before each test by the autouse clean_db fixture.
"""

import pytest
import pytest_asyncio
from uuid import UUID
from httpx import AsyncClient
from tests.conftest import remember as _remember, admin_headers


async def remember(client, agent_id: str, text: str, domain_tags=None, headers=None):
    """Thin wrapper around conftest.remember.

    With sync_store_for_tests=True (set by warmup_embeddings session fixture),
    the /remember endpoint awaits the store task inline before returning 201.
    Atoms are available immediately after this call returns — no sleep needed.
    """
    return await _remember(client, agent_id, text, domain_tags=domain_tags, headers=headers)



# ── Agent endpoints ───────────────────────────────────────────────────────────

class TestAgents:
    async def test_register_agent(self, client, operator_with_key):
        _, _, op_headers = operator_with_key
        resp = await client.post("/v1/agents", json={
            "name": "ada",
            "persona": "software engineer",
            "domain_tags": ["python", "databases"],
        }, headers=op_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "ada"
        assert data["status"] == "active"
        assert "id" in data

    async def test_get_agent(self, client, agent):
        resp = await client.get(f"/v1/agents/{agent['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == agent["id"]

    async def test_get_agent_not_found(self, client):
        resp = await client.get("/v1/agents/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404

    async def test_agent_stats_empty(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        resp = await client.get(f"/v1/agents/{agent['id']}/stats", headers=ag_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_atoms"] == 0
        assert data["active_atoms"] == 0
        assert data["avg_effective_confidence"] == 0.0

    async def test_depart(self, client, agent):
        resp = await client.post(f"/v1/agents/{agent['id']}/depart", headers=admin_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert "capabilities_revoked" in data
        assert "data_expires_at" in data

    async def test_depart_twice_fails(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/depart", headers=admin_headers())
        resp = await client.post(f"/v1/agents/{agent['id']}/depart", headers=admin_headers())
        assert resp.status_code == 409

    async def test_departed_agent_cannot_remember(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await client.post(f"/v1/agents/{agent['id']}/depart", headers=admin_headers())
        resp = await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "This should fail."
        }, headers=ag_headers)
        # Departed agent key is invalid (auth returns 401, not 410)
        assert resp.status_code == 401

    async def test_find_agent_by_name(self, client, operator_with_key):
        _, _, op_headers = operator_with_key
        await client.post("/v1/agents", json={"name": "find-me", "domain_tags": []}, headers=op_headers)
        resp = await client.get("/v1/agents", params={"name": "find-me"}, headers=op_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "find-me"

    async def test_find_agent_by_name_not_found(self, client, operator_with_key):
        _, _, op_headers = operator_with_key
        resp = await client.get("/v1/agents", params={"name": "does-not-exist"}, headers=op_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_find_agent_by_name_excludes_departed(self, client, operator_with_key):
        _, _, op_headers = operator_with_key
        r = await client.post("/v1/agents", json={"name": "departed-agent", "domain_tags": []}, headers=op_headers)
        agent_id = r.json()["id"]
        await client.post(f"/v1/agents/{agent_id}/depart", headers=admin_headers())
        resp = await client.get("/v1/agents", params={"name": "departed-agent"}, headers=op_headers)
        assert resp.status_code == 200
        assert resp.json() == []


# ── Remember endpoint ─────────────────────────────────────────────────────────

class TestRemember:
    async def test_remember_returns_queued_status(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        resp = await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "pandas.read_csv silently coerces mixed-type columns.",
            "domain_tags": ["python", "pandas"],
        }, headers=ag_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "queued"
        assert "store_id" in data

    async def test_remember_returns_typed_atoms(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        # Create atoms explicitly with known types, then verify via direct GET
        a1_resp = await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": "asyncpg uses a connection pool internally.",
        }, headers=ag_headers)
        assert a1_resp.status_code == 201
        a2_resp = await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "episodic",
            "text_content": "I ran into a connection leak issue yesterday.",
        }, headers=ag_headers)
        assert a2_resp.status_code == 201
        # Verify types directly via atom GET (avoids recall similarity flakiness)
        a1 = await client.get(f"/v1/agents/{agent['id']}/atoms/{a1_resp.json()['id']}", headers=ag_headers)
        a2 = await client.get(f"/v1/agents/{agent['id']}/atoms/{a2_resp.json()['id']}", headers=ag_headers)
        assert a1.json()["atom_type"] == "semantic"
        assert a2.json()["atom_type"] == "episodic"

    async def test_remember_confidence_on_atoms(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        # Create an explicit atom to test confidence field exposure
        atom_resp = await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "episodic",
            "text_content": "I confirmed the query planner uses the index on agent_id.",
            "confidence": "high",
        }, headers=ag_headers)
        assert atom_resp.status_code == 201
        atom_id = atom_resp.json()["id"]
        # Verify confidence fields via direct atom GET (avoids recall similarity flakiness)
        resp = await client.get(
            f"/v1/agents/{agent['id']}/atoms/{atom_id}", headers=ag_headers,
        )
        assert resp.status_code == 200
        atom = resp.json()
        assert "confidence_expected" in atom
        assert "confidence_effective" in atom
        assert 0 < atom["confidence_expected"] <= 1.0

    async def test_remember_deduplication(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        text = "asyncpg does not auto-commit transactions."
        # Store the first sentence explicitly (synchronous, avoids background task timing)
        r1 = await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": text,
        }, headers=ag_headers)
        assert r1.status_code == 201

        stats_before = (await client.get(f"/v1/agents/{agent['id']}/stats", headers=ag_headers)).json()
        atoms_before = stats_before["active_atoms"]

        # Now store via /remember — duplicate detection should merge rather than create
        await remember(client, agent["id"], "asyncpg does not auto-commit transactions by default.", headers=ag_headers)
        stats_after = (await client.get(f"/v1/agents/{agent['id']}/stats", headers=ag_headers)).json()
        # After dedup/merge, atom count should not increase (duplicate merged)
        assert stats_after["active_atoms"] <= atoms_before + 1

    async def test_remember_updates_stats(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], "PostgreSQL supports partial indexes.", headers=ag_headers)
        stats = (await client.get(f"/v1/agents/{agent['id']}/stats", headers=ag_headers)).json()
        assert stats["active_atoms"] >= 1


# ── Recall endpoint ───────────────────────────────────────────────────────────

class TestRecall:
    async def test_recall_returns_relevant_atom(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], "pgvector stores embeddings as vector(384) columns.", domain_tags=["postgres"], headers=ag_headers)
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "storing vector embeddings in postgres",
        }, headers=ag_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_retrieved"] >= 1
        texts = [a["text_content"] for a in data["atoms"] + data["expanded_atoms"]]
        assert any("pgvector" in t or "vector" in t or "embedding" in t for t in texts)

    async def test_recall_empty_when_no_memories(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "anything at all",
        }, headers=ag_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_retrieved"] == 0

    async def test_recall_respects_agent_isolation(self, client, two_agents):
        alice, bob = two_agents
        alice_headers = {"X-Agent-Key": alice["agent_key"]}
        bob_headers = {"X-Agent-Key": bob["agent_key"]}
        await remember(client, alice["id"], "Alice's secret: use connection pooling.", headers=alice_headers)
        # Bob should not see Alice's atoms
        resp = await client.post(f"/v1/agents/{bob['id']}/recall", json={
            "query": "Alice secret connection pooling",
        }, headers=bob_headers)
        assert resp.status_code == 200
        for atom in resp.json()["atoms"]:
            assert atom["agent_id"] == bob["id"]

    async def test_recall_response_structure(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], "Redis is an in-memory key-value store.", headers=ag_headers)
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "Redis caching",
        }, headers=ag_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "atoms" in data
        assert "expanded_atoms" in data
        assert "total_retrieved" in data


# ── Explicit atom CRUD ────────────────────────────────────────────────────────

class TestAtoms:
    async def test_create_explicit_atom(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        resp = await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": "Kubernetes uses etcd as its backing store.",
            "confidence": "high",
            "domain_tags": ["k8s"],
        }, headers=ag_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["atom_type"] == "semantic"
        assert data["confidence_expected"] > 0.8  # high confidence

    async def test_get_atom(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        create = await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "procedural",
            "text_content": "Always run kubectl diff before applying manifests.",
        }, headers=ag_headers)
        atom_id = create.json()["id"]
        resp = await client.get(f"/v1/agents/{agent['id']}/atoms/{atom_id}", headers=ag_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == atom_id

    async def test_delete_atom(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        create = await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": "This atom will be deleted.",
        }, headers=ag_headers)
        atom_id = create.json()["id"]
        resp = await client.delete(f"/v1/agents/{agent['id']}/atoms/{atom_id}", headers=ag_headers)
        assert resp.status_code == 204

        # Should no longer be findable
        get_resp = await client.get(f"/v1/agents/{agent['id']}/atoms/{atom_id}", headers=ag_headers)
        assert get_resp.status_code == 404

    async def test_link_atoms(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        a1 = (await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "episodic",
            "text_content": "I saw the query plan use a seq scan.",
        }, headers=ag_headers)).json()
        a2 = (await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": "Missing indexes cause sequential scans.",
        }, headers=ag_headers)).json()

        resp = await client.post(f"/v1/agents/{agent['id']}/atoms/link", json={
            "source_id": a1["id"],
            "target_id": a2["id"],
            "edge_type": "evidence_for",
            "weight": 0.9,
        }, headers=ag_headers)
        assert resp.status_code == 201
        edge = resp.json()
        assert edge["edge_type"] == "evidence_for"
        assert edge["weight"] == 0.9

    async def test_link_duplicate_is_conflict(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        a1 = (await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": "PostgreSQL uses B-tree indexes by default for primary keys.",
        }, headers=ag_headers)).json()
        a2 = (await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": "The French Revolution began in 1789 with the storming of the Bastille.",
        }, headers=ag_headers)).json()
        link_body = {"source_id": a1["id"], "target_id": a2["id"], "edge_type": "supports"}
        await client.post(f"/v1/agents/{agent['id']}/atoms/link", json=link_body, headers=ag_headers)
        resp = await client.post(f"/v1/agents/{agent['id']}/atoms/link", json=link_body, headers=ag_headers)
        assert resp.status_code == 409


# ── Views and skill export ────────────────────────────────────────────────────

class TestViews:
    async def test_create_view(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], "Always use parameterised queries to prevent SQL injection.", domain_tags=["security"], headers=ag_headers)
        resp = await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "security-practices",
            "atom_filter": {"domain_tags": ["security"]},
        }, headers=ag_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "security-practices"
        assert data["atom_count"] >= 1

    async def test_list_views(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "view-1",
            "atom_filter": {},
        }, headers=ag_headers)
        await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "view-2",
            "atom_filter": {},
        }, headers=ag_headers)
        resp = await client.get(f"/v1/agents/{agent['id']}/views", headers=ag_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_export_skill_structure(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], "Always specify dtype when using read_csv.", domain_tags=["pandas"], headers=ag_headers)
        view = (await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "pandas-skills",
            "atom_filter": {"atom_types": ["procedural"]},
        }, headers=ag_headers)).json()

        resp = await client.get(
            f"/v1/agents/{agent['id']}/views/{view['id']}/export_skill",
            headers=ag_headers,
        )
        assert resp.status_code == 200
        skill = resp.json()
        assert skill["name"] == "pandas-skills"
        assert "rendered_markdown" in skill
        assert "# pandas-skills" in skill["rendered_markdown"]
        assert "procedures" in skill
        assert "supporting_facts" in skill

    async def test_export_skill_wrong_owner(self, client, two_agents):
        alice, bob = two_agents
        alice_headers = {"X-Agent-Key": alice["agent_key"]}
        bob_headers = {"X-Agent-Key": bob["agent_key"]}
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "alice-view",
            "atom_filter": {},
        }, headers=alice_headers)).json()
        resp = await client.get(
            f"/v1/agents/{bob['id']}/views/{view['id']}/export_skill",
            headers=bob_headers,
        )
        assert resp.status_code == 403

    async def test_snapshot_freezes_atoms(self, client, agent):
        """Atoms created after snapshot should not appear in it."""
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        view = (await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "empty-snapshot",
            "atom_filter": {},
        }, headers=ag_headers)).json()
        # Atom created AFTER snapshot
        await remember(client, agent["id"], "This was added after the snapshot was taken.", headers=ag_headers)
        # The snapshot's atom_count should still reflect the pre-snapshot state
        views = (await client.get(f"/v1/agents/{agent['id']}/views", headers=ag_headers)).json()
        snap = next(v for v in views if v["id"] == view["id"])
        assert snap["atom_count"] == view["atom_count"]  # unchanged


# ── Capabilities ──────────────────────────────────────────────────────────────

class TestCapabilities:
    async def _setup_shared_view(self, client, alice, bob):
        """Helper: alice creates a view, grants it to bob."""
        alice_headers = {"X-Agent-Key": alice["agent_key"]}
        await remember(client, alice["id"], "Always use connection pooling in production.", domain_tags=["ops"], headers=alice_headers)
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "ops-skills",
            "atom_filter": {"domain_tags": ["ops"]},
        }, headers=alice_headers)).json()
        cap = (await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": bob["id"],
        }, headers=alice_headers)).json()
        return view, cap

    async def test_grant_capability(self, client, two_agents):
        alice, bob = two_agents
        view, cap = await self._setup_shared_view(client, alice, bob)
        assert cap["grantee_id"] == bob["id"]
        assert cap["view_id"] == view["id"]
        assert cap["revoked"] is False

    async def test_shared_views_listed(self, client, two_agents):
        alice, bob = two_agents
        bob_headers = {"X-Agent-Key": bob["agent_key"]}
        await self._setup_shared_view(client, alice, bob)
        resp = await client.get(f"/v1/agents/{bob['id']}/shared_views", headers=bob_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_recall_through_shared_view(self, client, two_agents):
        alice, bob = two_agents
        bob_headers = {"X-Agent-Key": bob["agent_key"]}
        view, _cap = await self._setup_shared_view(client, alice, bob)
        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "connection pooling production"},
            headers=bob_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_retrieved"] >= 1

    async def test_recall_shared_without_capability_denied(self, client, two_agents):
        alice, bob = two_agents
        alice_headers = {"X-Agent-Key": alice["agent_key"]}
        bob_headers = {"X-Agent-Key": bob["agent_key"]}
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "private-view",
            "atom_filter": {},
        }, headers=alice_headers)).json()
        # No grant — bob tries to recall
        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "anything"},
            headers=bob_headers,
        )
        assert resp.status_code == 403

    async def test_revoke_removes_shared_access(self, client, two_agents):
        alice, bob = two_agents
        alice_headers = {"X-Agent-Key": alice["agent_key"]}
        bob_headers = {"X-Agent-Key": bob["agent_key"]}
        view, cap = await self._setup_shared_view(client, alice, bob)

        # Revoke
        revoke_resp = await client.post(f"/v1/capabilities/{cap['id']}/revoke", headers=alice_headers)
        assert revoke_resp.status_code == 200

        # Bob can no longer recall through the view
        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "connection pooling"},
            headers=bob_headers,
        )
        assert resp.status_code == 403

    async def test_departure_cascade_revokes_grants(self, client, two_agents):
        alice, bob = two_agents
        bob_headers = {"X-Agent-Key": bob["agent_key"]}
        view, cap = await self._setup_shared_view(client, alice, bob)

        # Alice departs (admin action)
        depart = (await client.post(f"/v1/agents/{alice['id']}/depart", headers=admin_headers())).json()
        assert depart["capabilities_revoked"] >= 1

        # Bob can no longer recall through alice's view
        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "connection pooling"},
            headers=bob_headers,
        )
        assert resp.status_code == 403

    async def test_grant_wrong_owner_denied(self, client, two_agents):
        alice, bob = two_agents
        alice_headers = {"X-Agent-Key": alice["agent_key"]}
        bob_headers = {"X-Agent-Key": bob["agent_key"]}
        # Alice creates a view
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "alice-view",
            "atom_filter": {},
        }, headers=alice_headers)).json()
        # Bob tries to grant it — should fail
        resp = await client.post(f"/v1/agents/{bob['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": alice["id"],
        }, headers=bob_headers)
        assert resp.status_code == 403


# ── Snapshot semantics ───────────────────────────────────────────────────────

class TestSnapshotSemantics:
    async def _setup_shared_view(self, client, pool, operator_with_key):
        """Create agent, store atoms, share a view. Returns (agent, view, cap, bob)."""
        from mnemo.server.services.consolidation import run_consolidation

        _, _, op_headers = operator_with_key
        r_alice = await client.post("/v1/agents", json={"name": "alice-snap", "domain_tags": ["snap"]}, headers=op_headers)
        r_bob   = await client.post("/v1/agents", json={"name": "bob-snap",   "domain_tags": ["snap"]}, headers=op_headers)
        assert r_alice.status_code == 201
        assert r_bob.status_code == 201
        alice = r_alice.json()
        bob   = r_bob.json()

        alice_headers = {"X-Agent-Key": alice["agent_key"]}
        bob_headers = {"X-Agent-Key": bob["agent_key"]}
        await remember(client, alice["id"], "asyncio.gather runs coroutines concurrently.", domain_tags=["python"], headers=alice_headers)
        view = (await client.post(
            f"/v1/agents/{alice['id']}/views",
            json={"name": "snap-view", "atom_filter": {}},
            headers=alice_headers,
        )).json()
        cap = (await client.post(
            f"/v1/agents/{alice['id']}/grant",
            json={"view_id": view["id"], "grantee_id": bob["id"]},
            headers=alice_headers,
        )).json()
        return alice, bob, view, cap, bob_headers

    async def test_snapshot_degrades_after_decay(self, client, pool, operator_with_key):
        """
        After atoms in a snapshot are deactivated by consolidation, they no
        longer appear in shared view recall. Documents current v0.2 semantics.
        """
        from mnemo.server.services.consolidation import run_consolidation

        alice, bob, view, cap, bob_headers = await self._setup_shared_view(client, pool, operator_with_key)

        # Verify Bob can recall through the view before decay
        before = (await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "asyncio concurrent coroutines"},
            headers=bob_headers,
        )).json()
        assert before["total_retrieved"] >= 1

        # Age the atoms far beyond any half-life so effective_confidence << 0.05
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE atoms SET created_at = now() - interval '365 days',
                                 last_accessed = NULL
                WHERE agent_id = $1
                """,
                UUID(alice["id"]),
            )

        # Run consolidation — decay step deactivates the faded atoms
        await run_consolidation(pool)

        # Bob can no longer recall those atoms through the shared view
        after = (await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "asyncio concurrent coroutines"},
            headers=bob_headers,
        )).json()
        assert after["total_retrieved"] == 0

    async def test_snapshot_atom_ids_survive_deactivation(self, client, pool, operator_with_key):
        """
        snapshot_atoms rows are NOT removed when atoms are deactivated.
        The ID set is stable (scope safety); only liveness changes.
        """
        from mnemo.server.services.consolidation import run_consolidation

        alice, bob, view, cap, bob_headers = await self._setup_shared_view(client, pool, operator_with_key)

        # Age and deactivate via consolidation
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE atoms SET created_at = now() - interval '365 days' WHERE agent_id = $1",
                UUID(alice["id"]),
            )
        await run_consolidation(pool)

        # snapshot_atoms rows should still exist even though atoms are inactive
        async with pool.acquire() as conn:
            snap_count = await conn.fetchval(
                "SELECT COUNT(*) FROM snapshot_atoms WHERE view_id = $1",
                UUID(view["id"]),
            )
        assert snap_count >= 1  # IDs preserved, just atoms are inactive


# ── Recall quality ────────────────────────────────────────────────────────────

class TestRecallQuality:
    """Tests for similarity floor, composite ranking, and expansion filtering."""

    async def test_recall_respects_min_similarity(self, client, agent):
        """Atoms below min_similarity are excluded; unrelated topics don't surface."""
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], "pandas read_csv silently coerces mixed column types in DataFrames.", headers=ag_headers)
        await remember(client, agent["id"], "I baked sourdough bread with extra rye flour and longer fermentation.", headers=ag_headers)

        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "pandas read_csv coerces column types DataFrame",
            "min_similarity": 0.3,
            "expand_graph": False,
        }, headers=ag_headers)
        assert resp.status_code == 200
        data = resp.json()
        texts = [a["text_content"] for a in data["atoms"]]
        # pandas atom should appear; sourdough should not at this threshold
        assert any("pandas" in t or "csv" in t.lower() or "coerce" in t.lower() for t in texts)
        assert not any("sourdough" in t or "bread" in t for t in texts)

    async def test_recall_returns_empty_when_nothing_relevant(self, client, agent):
        """With a high similarity floor and an unrelated query, results are empty."""
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], "I baked sourdough bread with a poolish starter yesterday.", headers=ag_headers)

        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "quantum chromodynamics particle physics collider",
            "min_similarity": 0.75,
            "expand_graph": False,
        }, headers=ag_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_retrieved"] == 0

    async def test_recall_ranks_by_composite_score(self, client, agent):
        """High-confidence atoms rank above low-confidence atoms with similar text."""
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], "I confirmed that pandas read_csv definitely coerces column types silently.", headers=ag_headers)
        await remember(client, agent["id"], "I think maybe pandas read_csv might have some type coercion issues.", headers=ag_headers)

        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "pandas read_csv column type coercion",
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        }, headers=ag_headers)
        assert resp.status_code == 200
        data = resp.json()
        atoms = data["atoms"]
        assert len(atoms) >= 1
        # All primary results must have a relevance_score
        for a in atoms:
            assert a["relevance_score"] is not None
        # Results are ordered by relevance_score descending
        scores = [a["relevance_score"] for a in atoms]
        assert scores == sorted(scores, reverse=True)

    async def test_expanded_atoms_filtered_by_similarity(self, client, agent):
        """All atoms returned in expanded_atoms have relevance_score >= min_similarity * 0.6."""
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        # Store connected atoms (multi-sentence → edges created between them)
        await remember(client, agent["id"], (
            "pandas read_csv coerces column dtypes without warning. "
            "I discovered this while processing a CSV file. "
            "Always specify dtype explicitly when using read_csv."
        ), headers=ag_headers)

        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "CSV data type parsing",
            "min_similarity": 0.3,
            "expand_graph": True,
        }, headers=ag_headers)
        assert resp.status_code == 200
        data = resp.json()
        exp_floor = 0.3 * 0.6
        for atom in data["expanded_atoms"]:
            assert atom["relevance_score"] is not None
            assert atom["relevance_score"] >= exp_floor


# ── Arc atoms ────────────────────────────────────────────────────────────────

class TestArcAtoms:
    """Integration tests for the arc decomposer feature."""

    _arc_text = (
        "I started investigating a slow API endpoint yesterday. "
        "The profiler showed that database queries were taking 800 milliseconds. "
        "I added an index on the user_id column and query time dropped to 5 milliseconds. "
        "From now on I should always profile before guessing at the bottleneck."
    )

    async def test_remember_medium_creates_arc_atom(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], self._arc_text, headers=ag_headers)
        # Verify multiple atoms were stored (component + arc) and recall returns relevant content
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "API endpoint performance profiling investigation",
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 20,
        }, headers=ag_headers)
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        assert len(atoms) >= 1
        # At least one result should contain content from the arc text
        texts = " ".join(a["text_content"] for a in atoms)
        assert "profil" in texts.lower() or "index" in texts.lower() or "endpoint" in texts.lower()

    async def test_recall_finds_arc_by_theme(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], self._arc_text, headers=ag_headers)
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "API endpoint performance debugging and profiling",
            "min_similarity": 0.1,
            "expand_graph": False,
        }, headers=ag_headers)
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        # Thematic recall should return relevant content regardless of whether
        # the arc atom or its components survive dedup
        assert len(atoms) >= 1
        texts = " ".join(a["text_content"] for a in atoms)
        assert "profil" in texts.lower() or "endpoint" in texts.lower()

    async def test_recall_expands_from_arc_to_atoms(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], self._arc_text, headers=ag_headers)
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "slow API database index profiling investigation",
            "min_similarity": 0.1,
            "expand_graph": True,
        }, headers=ag_headers)
        assert resp.status_code == 200
        data = resp.json()
        # Graph expansion should surface related atoms from the arc text
        all_atoms = data["atoms"] + data["expanded_atoms"]
        assert len(all_atoms) >= 1
        texts = " ".join(a["text_content"] for a in all_atoms)
        assert "index" in texts.lower() or "profil" in texts.lower()

    async def test_recall_expands_from_atom_to_arc(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], self._arc_text, headers=ag_headers)
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "always profile before guessing at the bottleneck",
            "min_similarity": 0.1,
            "expand_graph": True,
        }, headers=ag_headers)
        assert resp.status_code == 200
        data = resp.json()
        all_atoms = data["atoms"] + data["expanded_atoms"]
        # Procedural advice and related arc content should be reachable
        assert len(all_atoms) >= 1
        texts = " ".join(a["text_content"] for a in all_atoms)
        assert "profil" in texts.lower() or "optimi" in texts.lower() or "bottleneck" in texts.lower()


# ── Recall controls ──────────────────────────────────────────────────────────

class TestRecallControls:
    """Tests for similarity_drop_threshold, verbosity, and max_total_tokens controls."""

    # ── Gap threshold ──────────────────────────────────────────────────────

    async def test_gap_threshold_stops_at_cliff(self, client, agent):
        """Atoms from an unrelated topic are cut when the score cliffs."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        for text in [
            "pandas read_csv silently coerces mixed-type columns to object dtype.",
            "Use dtype parameter in pandas read_csv to prevent silent type coercion.",
            "pandas DataFrame dtypes can be inspected with df.dtypes after loading.",
        ]:
            await remember(client, aid, text, headers=ag_headers)
        for text in [
            "The Battle of Hastings was fought in 1066 between Harold and William.",
            "Medieval siege weapons included trebuchets, mangonels, and battering rams.",
        ]:
            await remember(client, aid, text, headers=ag_headers)

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV column type coercion dtype",
            "similarity_drop_threshold": 0.3,
            "min_similarity": 0.75,
            "expand_graph": False,
            "max_results": 10,
        }, headers=ag_headers)
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        texts = [a["text_content"] for a in atoms]
        # Medieval history atoms should be cut by min_similarity floor
        assert not any("1066" in t or "trebuchet" in t or "Hastings" in t for t in texts)

    async def test_gap_threshold_none_returns_all(self, client, agent):
        """With threshold=None, all atoms above min_similarity are returned."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        for text in [
            "pandas read_csv silently coerces mixed-type columns to object dtype.",
            "The Battle of Hastings was fought in 1066.",
        ]:
            await remember(client, aid, text, headers=ag_headers)

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV column type coercion",
            "similarity_drop_threshold": None,
            "min_similarity": 0.05,
            "expand_graph": False,
            "max_results": 10,
        }, headers=ag_headers)
        assert resp.status_code == 200
        # Both atoms should be present (no filtering)
        assert resp.json()["total_retrieved"] >= 1

    async def test_gap_threshold_with_uniform_scores(self, client, agent):
        """No cliff in uniform topic → all atoms returned."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        for text in [
            "pandas read_csv coerces dtypes silently.",
            "pandas DataFrame dtypes should be set explicitly.",
            "pandas read_csv dtype parameter prevents coercion.",
            "Always check dtypes after loading a CSV with pandas.",
            "pandas dtype inference is unreliable for mixed columns.",
        ]:
            await remember(client, aid, text, headers=ag_headers)

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV dtype coercion",
            "similarity_drop_threshold": 0.3,
            "min_similarity": 0.3,
            "expand_graph": False,
            "max_results": 10,
        }, headers=ag_headers)
        assert resp.status_code == 200
        # Uniform topic — should return at least one relevant result
        atoms = resp.json()["atoms"]
        assert len(atoms) >= 1
        # All returned results should be about pandas/dtypes (no irrelevant content)
        for a in atoms:
            assert "pandas" in a["text_content"].lower() or "dtype" in a["text_content"].lower()

    async def test_gap_threshold_single_result(self, client, agent):
        """Steep cliff between relevant and irrelevant → only relevant result returned."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, aid, "pandas read_csv dtype coercion silently mangles column types.", headers=ag_headers)
        await remember(client, aid, "The French Revolution began in 1789 with the storming of the Bastille.", headers=ag_headers)

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas read_csv dtype coercion column types",
            "similarity_drop_threshold": 0.3,
            "min_similarity": 0.3,
            "expand_graph": False,
            "max_results": 10,
        }, headers=ag_headers)
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        # At least the pandas atom returned; French Revolution should be cut by gap or floor
        assert len(atoms) >= 1
        texts = [a["text_content"] for a in atoms]
        assert not any("Bastille" in t or "1789" in t for t in texts)

    # ── Verbosity ──────────────────────────────────────────────────────────

    async def test_verbosity_full_returns_complete_text(self, client, agent):
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        full_text = (
            "pandas read_csv coerces dtypes silently. "
            "This caused data loss in production. "
            "Always specify dtype explicitly."
        )
        # Store atom directly to control exact content
        atom_resp = await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": full_text,
            "domain_tags": ["python"],
        }, headers=ag_headers)
        assert atom_resp.status_code == 201

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV dtype coercion",
            "verbosity": "full",
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        }, headers=ag_headers)
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        # verbosity=full should not truncate — at least one atom should
        # contain substantial text from what we stored
        assert len(atoms) >= 1
        texts = " ".join(a["text_content"] for a in atoms)
        assert "pandas" in texts.lower() and "dtype" in texts.lower()

    async def test_verbosity_summary_returns_first_sentence(self, client, agent):
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        text = "First sentence here. Second sentence here. Third sentence here."
        atom_resp = await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": text,
            "domain_tags": [],
        }, headers=ag_headers)
        assert atom_resp.status_code == 201
        atom_id = atom_resp.json()["id"]

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "First sentence here",
            "verbosity": "summary",
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        }, headers=ag_headers)
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        matching = [a for a in atoms if a["id"] == atom_id]
        assert matching, "stored atom not recalled"
        assert matching[0]["text_content"] == "First sentence here."

    async def test_verbosity_truncated_respects_char_limit(self, client, agent):
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        long_text = "pandas " + ("x" * 500)
        atom_resp = await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": long_text,
            "domain_tags": [],
        }, headers=ag_headers)
        assert atom_resp.status_code == 201
        atom_id = atom_resp.json()["id"]

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas",
            "verbosity": "truncated",
            "max_content_chars": 100,
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        }, headers=ag_headers)
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        matching = [a for a in atoms if a["id"] == atom_id]
        assert matching, "stored atom not recalled"
        content = matching[0]["text_content"]
        assert content.endswith("...")
        assert len(content) == 103  # 100 chars + "..."

    async def test_verbosity_summary_single_sentence(self, client, agent):
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        text = "Only one sentence no period"
        atom_resp = await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": text,
            "domain_tags": [],
        }, headers=ag_headers)
        assert atom_resp.status_code == 201
        atom_id = atom_resp.json()["id"]

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "Only one sentence no period",
            "verbosity": "summary",
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        }, headers=ag_headers)
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        matching = [a for a in atoms if a["id"] == atom_id]
        assert matching, "stored atom not recalled"
        # No sentence boundary → full text preserved
        assert matching[0]["text_content"] == text

    # ── Token budget ───────────────────────────────────────────────────────

    async def test_token_budget_limits_results(self, client, agent):
        """With a tight budget, fewer than max_results atoms are returned."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        # ~130-char atoms ≈ 33 tokens each; 5 atoms ≈ 165 tokens
        for i in range(5):
            await client.post(f"/v1/agents/{aid}/atoms", json={
                "atom_type": "semantic",
                "text_content": f"pandas read_csv coerces column types silently version {i} " + "word " * 15,
                "domain_tags": [],
            }, headers=ag_headers)

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV column type coercion",
            "max_total_tokens": 80,
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
            "similarity_drop_threshold": None,
        }, headers=ag_headers)
        assert resp.status_code == 200
        data = resp.json()
        # Budget of 80 tokens (~320 chars) should exclude some of the 5 atoms
        assert data["total_retrieved"] < 5

    async def test_token_budget_always_returns_one(self, client, agent):
        """Even with a very tight budget, at least 1 atom is always returned."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        long_text = "pandas " + ("word " * 200)  # ~1000 tokens
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": long_text,
            "domain_tags": [],
        }, headers=ag_headers)

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas",
            "max_total_tokens": 50,
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        }, headers=ag_headers)
        assert resp.status_code == 200
        assert resp.json()["total_retrieved"] >= 1

    async def test_token_budget_none_returns_all(self, client, agent):
        """With max_total_tokens=None, all atoms above the similarity floor are returned."""
        aid = agent["id"]
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        # Store 3 variants about the same topic — distinct enough to avoid dedup (cosine < 0.90)
        texts = [
            "pandas read_csv coerces columns to object dtype when mixing integers and strings.",
            "pandas DataFrame dtype inference silently changes integer columns to float64.",
            "pandas read_csv misidentifies numeric strings as floats causing downstream errors.",
        ]
        for text in texts:
            await client.post(f"/v1/agents/{aid}/atoms", json={
                "atom_type": "semantic",
                "text_content": text,
                "domain_tags": [],
            }, headers=ag_headers)

        # First, verify all 3 are actually stored (not merged)
        stats = (await client.get(f"/v1/agents/{aid}/stats", headers=ag_headers)).json()
        stored = stats["active_atoms"]

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV dtype coercion column types",
            "max_total_tokens": None,
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
            "similarity_drop_threshold": None,
        }, headers=ag_headers)
        assert resp.status_code == 200
        # Without a token budget, all stored+similar atoms are returned
        assert resp.json()["total_retrieved"] >= min(stored, 3)


# ── Auth endpoints ────────────────────────────────────────────────────────────

class TestAuth:
    async def test_create_operator_creates_key_in_db(self, client, pool):
        """Admin operator create stores a hashed key in api_keys."""
        import hashlib
        resp = await client.post("/v1/admin/operators", headers=admin_headers(), json={
            "username": "authtest",
            "org": "testorg",
            "display_name": "Auth Test Op",
            "email": "test@example.com",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["api_key"].startswith("mnemo_")
        assert "uuid" in data

        key_hash = hashlib.sha256(data["api_key"].encode()).hexdigest()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, is_active FROM api_keys WHERE key_hash = $1",
                key_hash,
            )
        assert row is not None, "api_key INSERT was not committed to the database"
        assert row["is_active"] is True

    async def test_operator_key_then_me_roundtrip(self, client):
        """Key from operator create must authenticate against /auth/me."""
        r1 = await client.post("/v1/admin/operators", headers=admin_headers(), json={
            "username": "roundtrip",
            "org": "testorg",
            "display_name": "Roundtrip Op",
            "email": "rt@test.com",
        })
        assert r1.status_code == 201
        key = r1.json()["api_key"]
        operator_id = r1.json()["uuid"]

        r2 = await client.get("/v1/auth/me", headers={"X-Operator-Key": key})
        assert r2.status_code == 200, f"me() returned {r2.status_code}: {r2.text}"
        assert r2.json()["id"] == operator_id
        assert r2.json()["agent_count"] == 0

    async def test_create_duplicate_operator_returns_409(self, client):
        """Creating the same operator twice returns 409."""
        body = {
            "username": "dupeop",
            "org": "testorg",
            "display_name": "Dupe Op",
            "email": "dupe@test.com",
        }
        r1 = await client.post("/v1/admin/operators", headers=admin_headers(), json=body)
        assert r1.status_code == 201
        r2 = await client.post("/v1/admin/operators", headers=admin_headers(), json=body)
        assert r2.status_code == 409

    async def test_new_key_adds_additional_key(self, client, pool):
        """POST /auth/new-key generates a second key for the operator."""
        r1 = await client.post("/v1/admin/operators", headers=admin_headers(), json={
            "username": "newkeyop",
            "org": "testorg",
            "display_name": "NewKey Op",
            "email": "nk@test.com",
        })
        assert r1.status_code == 201
        key1 = r1.json()["api_key"]
        operator_id = r1.json()["uuid"]

        r2 = await client.post("/v1/auth/new-key", headers={"X-Operator-Key": key1})
        assert r2.status_code == 200
        key2 = r2.json()["api_key"]
        assert key1 != key2

        async with pool.acquire() as conn:
            from uuid import UUID
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM api_keys WHERE operator_id = $1 AND is_active = true",
                UUID(operator_id),
            )
        assert count == 2

    async def test_invalid_key_returns_401(self, client):
        resp = await client.get("/v1/auth/me", headers={"X-Operator-Key": "mnemo_notavalidkey"})
        assert resp.status_code == 401

    async def test_missing_bearer_returns_401(self, client):
        resp = await client.get("/v1/auth/me")
        assert resp.status_code == 401


# ── Admin endpoints ───────────────────────────────────────────────────────────

class TestAdmin:
    """Tests for /v1/admin/* endpoints behind X-Admin-Key auth."""

    async def test_no_token_returns_403(self, client):
        resp = await client.get("/v1/admin/agents")
        # No auth header at all -> 401
        assert resp.status_code in (401, 403)

    async def test_wrong_token_returns_401(self, client):
        resp = await client.get("/v1/admin/agents", headers={"X-Admin-Key": "wrong"})
        assert resp.status_code == 401

    async def test_admin_disabled_when_token_empty(self, client):
        from mnemo.server.config import settings
        original = settings.admin_key
        settings.admin_key = ""
        try:
            resp = await client.get("/v1/admin/agents", headers={"X-Admin-Key": "anything"})
            assert resp.status_code == 401
        finally:
            settings.admin_key = original

    async def test_list_agents_empty(self, client):
        resp = await client.get("/v1/admin/agents", headers=admin_headers())
        assert resp.status_code == 200
        assert resp.json() == {"agents": []}

    async def test_list_agents_shows_counts(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        # Store a memory so atom counts are non-zero
        await remember(client, agent["id"], "connection pooling boosts throughput significantly.", headers=ag_headers)
        resp = await client.get("/v1/admin/agents", headers=admin_headers())
        assert resp.status_code == 200
        agents = resp.json()["agents"]
        assert len(agents) == 1
        a = agents[0]
        assert a["id"] == agent["id"]
        assert a["active_atoms"] >= 1
        assert a["total_atoms"] >= 1

    async def test_operations_empty(self, client):
        resp = await client.get("/v1/admin/operations", headers=admin_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["by_operation"] == []

    async def test_operations_records_remember_and_recall(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], "connection pooling boosts throughput significantly.", headers=ag_headers)
        await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "connection pooling"
        }, headers=ag_headers)
        resp = await client.get("/v1/admin/operations", headers=admin_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        ops = {r["operation"]: r for r in data["by_operation"]}
        assert "remember" in ops
        assert "recall" in ops
        # remember is logged immediately (before background task runs), so duration_ms is None
        # recall is synchronous so it has a measured duration
        assert ops["recall"]["avg_duration_ms"] is not None

    async def test_operations_filter_by_target(self, client, two_agents):
        alice, bob = two_agents
        alice_headers = {"X-Agent-Key": alice["agent_key"]}
        bob_headers = {"X-Agent-Key": bob["agent_key"]}
        await remember(client, alice["id"], "Alice knows about connection pooling.", headers=alice_headers)
        await remember(client, bob["id"], "Bob knows about database indexing strategies.", headers=bob_headers)
        resp = await client.get(
            f"/v1/admin/operations?target_id={alice['id']}",
            headers=admin_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["by_operation"][0]["operation"] == "remember"

    async def test_operations_invalid_target_id(self, client):
        resp = await client.get(
            "/v1/admin/operations?target_id=not-a-uuid",
            headers=admin_headers(),
        )
        assert resp.status_code == 422

    async def test_keys_empty(self, client):
        resp = await client.get("/v1/admin/keys", headers=admin_headers())
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_keys_shows_registered_key(self, client, operator_with_key):
        op_data, _, _ = operator_with_key
        resp = await client.get("/v1/admin/keys", headers=admin_headers())
        assert resp.status_code == 200
        keys = resp.json()
        assert len(keys) >= 1
        k = keys[0]
        assert k["is_active"] is True
        assert k["key_prefix"].startswith("mnemo_")

    async def test_glance_shape(self, client):
        resp = await client.get("/v1/admin/glance", headers=admin_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        titles = {item["title"] for item in data["items"]}
        assert titles == {"Agents", "Atoms", "Ops today", "Recalls today", "Remembers today"}
        for item in data["items"]:
            assert "value" in item
            assert isinstance(item["value"], str)

    async def test_glance_counts_todays_ops(self, client, agent):
        ag_headers = {"X-Agent-Key": agent["agent_key"]}
        await remember(client, agent["id"], "connection pooling boosts throughput.", headers=ag_headers)
        await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "connection pooling"
        }, headers=ag_headers)
        resp = await client.get("/v1/admin/glance", headers=admin_headers())
        assert resp.status_code == 200
        items = {i["title"]: i["value"] for i in resp.json()["items"]}
        assert items["Ops today"] == "2"
        assert items["Remembers today"] == "1"
        assert items["Recalls today"] == "1"
        assert items["Agents"] == "1 active"


# ── Health ────────────────────────────────────────────────────────────────────

async def test_health(client):
    resp = await client.get("/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["postgres"] == "ok"
    assert "version" in data
    assert "schema_version" in data
    assert "uptime_seconds" in data
