"""
Integration tests for the Mnemo API.
Requires a running PostgreSQL instance (uses the configured MNEMO_DATABASE_URL).
Tables are truncated before each test by the autouse clean_db fixture.
"""

import pytest
from httpx import AsyncClient


# ── Agent endpoints ───────────────────────────────────────────────────────────

class TestAgents:
    async def test_register_agent(self, client):
        resp = await client.post("/v1/agents", json={
            "name": "ada",
            "persona": "software engineer",
            "domain_tags": ["python", "databases"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "ada"
        assert data["is_active"] is True
        assert "id" in data

    async def test_get_agent(self, client, agent):
        resp = await client.get(f"/v1/agents/{agent['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == agent["id"]

    async def test_get_agent_not_found(self, client):
        resp = await client.get("/v1/agents/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404

    async def test_agent_stats_empty(self, client, agent):
        resp = await client.get(f"/v1/agents/{agent['id']}/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_atoms"] == 0
        assert data["active_atoms"] == 0
        assert data["avg_effective_confidence"] == 0.0

    async def test_depart(self, client, agent):
        resp = await client.post(f"/v1/agents/{agent['id']}/depart")
        assert resp.status_code == 200
        data = resp.json()
        assert "capabilities_revoked" in data
        assert "data_expires_at" in data

    async def test_depart_twice_fails(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/depart")
        resp = await client.post(f"/v1/agents/{agent['id']}/depart")
        assert resp.status_code == 409

    async def test_departed_agent_cannot_remember(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/depart")
        resp = await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "This should fail."
        })
        assert resp.status_code == 410


# ── Remember endpoint ─────────────────────────────────────────────────────────

class TestRemember:
    async def test_remember_creates_atoms(self, client, agent):
        resp = await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": (
                "pandas.read_csv silently coerces mixed-type columns. "
                "I discovered this while processing client_data.csv. "
                "Always specify dtype explicitly when using read_csv."
            ),
            "domain_tags": ["python", "pandas"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["atoms_created"] >= 2
        assert data["edges_created"] >= 1
        assert len(data["atoms"]) >= 2

    async def test_remember_returns_typed_atoms(self, client, agent):
        resp = await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": (
                "asyncpg uses a connection pool internally. "
                "I ran into a connection leak issue yesterday."
            ),
        })
        assert resp.status_code == 201
        data = resp.json()
        types = {a["atom_type"] for a in data["atoms"]}
        assert "semantic" in types
        assert "episodic" in types

    async def test_remember_confidence_on_atoms(self, client, agent):
        resp = await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "I confirmed the query planner uses the index on agent_id.",
        })
        assert resp.status_code == 201
        atom = resp.json()["atoms"][0]
        # API should expose expected and effective, not raw alpha/beta
        assert "confidence_expected" in atom
        assert "confidence_effective" in atom
        assert "confidence_alpha" not in atom
        assert "confidence_beta" not in atom
        assert 0 < atom["confidence_expected"] <= 1.0

    async def test_remember_deduplication(self, client, agent):
        text = "asyncpg does not auto-commit transactions."
        # Store once
        r1 = await client.post(f"/v1/agents/{agent['id']}/remember", json={"text": text})
        assert r1.status_code == 201

        # Store a near-identical sentence
        r2 = await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "asyncpg does not auto-commit transactions by default."
        })
        assert r2.status_code == 201
        # Should detect the duplicate and merge
        assert r2.json()["duplicates_merged"] >= 1
        assert r2.json()["atoms_created"] == 0

    async def test_remember_updates_stats(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "PostgreSQL supports partial indexes."
        })
        stats = (await client.get(f"/v1/agents/{agent['id']}/stats")).json()
        assert stats["active_atoms"] >= 1


# ── Recall endpoint ───────────────────────────────────────────────────────────

class TestRecall:
    async def test_recall_returns_relevant_atom(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "pgvector stores embeddings as vector(384) columns.",
            "domain_tags": ["postgres"],
        })
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "storing vector embeddings in postgres",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_retrieved"] >= 1
        texts = [a["text_content"] for a in data["atoms"] + data["expanded_atoms"]]
        assert any("pgvector" in t or "vector" in t or "embedding" in t for t in texts)

    async def test_recall_empty_when_no_memories(self, client, agent):
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "anything at all",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_retrieved"] == 0

    async def test_recall_filters_by_atom_type(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": (
                "Indexes speed up queries significantly. "
                "Always add indexes on foreign keys."
            ),
        })
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "database indexes",
            "atom_types": ["procedural"],
        })
        assert resp.status_code == 200
        primary = resp.json()["atoms"]
        for atom in primary:
            assert atom["atom_type"] == "procedural"

    async def test_recall_respects_agent_isolation(self, client, two_agents):
        alice, bob = two_agents
        await client.post(f"/v1/agents/{alice['id']}/remember", json={
            "text": "Alice's secret: use connection pooling.",
        })
        # Bob should not see Alice's atoms
        resp = await client.post(f"/v1/agents/{bob['id']}/recall", json={
            "query": "Alice secret connection pooling",
        })
        assert resp.status_code == 200
        for atom in resp.json()["atoms"]:
            assert atom["agent_id"] == bob["id"]

    async def test_recall_response_structure(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "Redis is an in-memory key-value store.",
        })
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "Redis caching",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "atoms" in data
        assert "expanded_atoms" in data
        assert "total_retrieved" in data


# ── Explicit atom CRUD ────────────────────────────────────────────────────────

class TestAtoms:
    async def test_create_explicit_atom(self, client, agent):
        resp = await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": "Kubernetes uses etcd as its backing store.",
            "confidence": "high",
            "domain_tags": ["k8s"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["atom_type"] == "semantic"
        assert data["confidence_expected"] > 0.8  # high confidence

    async def test_get_atom(self, client, agent):
        create = await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "procedural",
            "text_content": "Always run kubectl diff before applying manifests.",
        })
        atom_id = create.json()["id"]
        resp = await client.get(f"/v1/agents/{agent['id']}/atoms/{atom_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == atom_id

    async def test_delete_atom(self, client, agent):
        create = await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": "This atom will be deleted.",
        })
        atom_id = create.json()["id"]
        resp = await client.delete(f"/v1/agents/{agent['id']}/atoms/{atom_id}")
        assert resp.status_code == 204

        # Should no longer be findable
        get_resp = await client.get(f"/v1/agents/{agent['id']}/atoms/{atom_id}")
        assert get_resp.status_code == 404

    async def test_link_atoms(self, client, agent):
        a1 = (await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "episodic",
            "text_content": "I saw the query plan use a seq scan.",
        })).json()
        a2 = (await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": "Missing indexes cause sequential scans.",
        })).json()

        resp = await client.post(f"/v1/agents/{agent['id']}/atoms/link", json={
            "source_id": a1["id"],
            "target_id": a2["id"],
            "edge_type": "evidence_for",
            "weight": 0.9,
        })
        assert resp.status_code == 201
        edge = resp.json()
        assert edge["edge_type"] == "evidence_for"
        assert edge["weight"] == 0.9

    async def test_link_duplicate_is_conflict(self, client, agent):
        a1 = (await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": "Fact A.",
        })).json()
        a2 = (await client.post(f"/v1/agents/{agent['id']}/atoms", json={
            "atom_type": "semantic",
            "text_content": "Fact B.",
        })).json()
        link_body = {"source_id": a1["id"], "target_id": a2["id"], "edge_type": "supports"}
        await client.post(f"/v1/agents/{agent['id']}/atoms/link", json=link_body)
        resp = await client.post(f"/v1/agents/{agent['id']}/atoms/link", json=link_body)
        assert resp.status_code == 409


# ── Views and skill export ────────────────────────────────────────────────────

class TestViews:
    async def test_create_view(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "Always use parameterised queries to prevent SQL injection.",
            "domain_tags": ["security"],
        })
        resp = await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "security-practices",
            "atom_filter": {"atom_types": ["procedural"], "domain_tags": ["security"]},
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "security-practices"
        assert data["atom_count"] >= 1

    async def test_list_views(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "view-1",
            "atom_filter": {},
        })
        await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "view-2",
            "atom_filter": {},
        })
        resp = await client.get(f"/v1/agents/{agent['id']}/views")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_export_skill_structure(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "Always specify dtype when using read_csv.",
            "domain_tags": ["pandas"],
        })
        view = (await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "pandas-skills",
            "atom_filter": {"atom_types": ["procedural"]},
        })).json()

        resp = await client.get(
            f"/v1/agents/{agent['id']}/views/{view['id']}/export_skill"
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
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "alice-view",
            "atom_filter": {},
        })).json()
        resp = await client.get(
            f"/v1/agents/{bob['id']}/views/{view['id']}/export_skill"
        )
        assert resp.status_code == 403

    async def test_snapshot_freezes_atoms(self, client, agent):
        """Atoms created after snapshot should not appear in it."""
        view = (await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "empty-snapshot",
            "atom_filter": {},
        })).json()
        # Atom created AFTER snapshot
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "This was added after the snapshot was taken.",
        })
        # The snapshot's atom_count should still reflect the pre-snapshot state
        views = (await client.get(f"/v1/agents/{agent['id']}/views")).json()
        snap = next(v for v in views if v["id"] == view["id"])
        assert snap["atom_count"] == view["atom_count"]  # unchanged


# ── Capabilities ──────────────────────────────────────────────────────────────

class TestCapabilities:
    async def _setup_shared_view(self, client, alice, bob):
        """Helper: alice creates a view, grants it to bob."""
        await client.post(f"/v1/agents/{alice['id']}/remember", json={
            "text": "Always use connection pooling in production.",
            "domain_tags": ["ops"],
        })
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "ops-skills",
            "atom_filter": {"domain_tags": ["ops"]},
        })).json()
        cap = (await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": bob["id"],
        })).json()
        return view, cap

    async def test_grant_capability(self, client, two_agents):
        alice, bob = two_agents
        view, cap = await self._setup_shared_view(client, alice, bob)
        assert cap["grantee_id"] == bob["id"]
        assert cap["view_id"] == view["id"]
        assert cap["revoked"] is False

    async def test_shared_views_listed(self, client, two_agents):
        alice, bob = two_agents
        await self._setup_shared_view(client, alice, bob)
        resp = await client.get(f"/v1/agents/{bob['id']}/shared_views")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_recall_through_shared_view(self, client, two_agents):
        alice, bob = two_agents
        view, _cap = await self._setup_shared_view(client, alice, bob)
        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "connection pooling production"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_retrieved"] >= 1

    async def test_recall_shared_without_capability_denied(self, client, two_agents):
        alice, bob = two_agents
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "private-view",
            "atom_filter": {},
        })).json()
        # No grant — bob tries to recall
        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "anything"},
        )
        assert resp.status_code == 403

    async def test_revoke_removes_shared_access(self, client, two_agents):
        alice, bob = two_agents
        view, cap = await self._setup_shared_view(client, alice, bob)

        # Revoke
        revoke_resp = await client.post(f"/v1/capabilities/{cap['id']}/revoke")
        assert revoke_resp.status_code == 200

        # Bob can no longer recall through the view
        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "connection pooling"},
        )
        assert resp.status_code == 403

    async def test_departure_cascade_revokes_grants(self, client, two_agents):
        alice, bob = two_agents
        view, cap = await self._setup_shared_view(client, alice, bob)

        # Alice departs
        depart = (await client.post(f"/v1/agents/{alice['id']}/depart")).json()
        assert depart["capabilities_revoked"] >= 1

        # Bob can no longer recall through alice's view
        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "connection pooling"},
        )
        assert resp.status_code == 403

    async def test_grant_wrong_owner_denied(self, client, two_agents):
        alice, bob = two_agents
        # Alice creates a view
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "alice-view",
            "atom_filter": {},
        })).json()
        # Bob tries to grant it — should fail
        resp = await client.post(f"/v1/agents/{bob['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": alice["id"],
        })
        assert resp.status_code == 403


# ── Health ────────────────────────────────────────────────────────────────────

async def test_health(client):
    resp = await client.get("/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
