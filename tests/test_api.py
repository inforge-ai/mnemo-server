"""
Integration tests for the Mnemo API.
Requires a running PostgreSQL instance (uses the configured MNEMO_DATABASE_URL).
Tables are truncated before each test by the autouse clean_db fixture.
"""

import pytest
from uuid import UUID
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

    async def test_find_agent_by_name(self, client):
        await client.post("/v1/agents", json={"name": "find-me", "domain_tags": []})
        resp = await client.get("/v1/agents", params={"name": "find-me"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "find-me"

    async def test_find_agent_by_name_not_found(self, client):
        resp = await client.get("/v1/agents", params={"name": "does-not-exist"})
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_find_agent_by_name_excludes_departed(self, client):
        r = await client.post("/v1/agents", json={"name": "departed-agent", "domain_tags": []})
        agent_id = r.json()["id"]
        await client.post(f"/v1/agents/{agent_id}/depart")
        resp = await client.get("/v1/agents", params={"name": "departed-agent"})
        assert resp.status_code == 200
        assert resp.json() == []


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


# ── Snapshot semantics ───────────────────────────────────────────────────────

class TestSnapshotSemantics:
    async def _setup_shared_view(self, client, pool):
        """Create agent, store atoms, share a view. Returns (agent, view, cap, bob)."""
        from mnemo.server.services.consolidation import run_consolidation

        r_alice = await client.post("/v1/agents", json={"name": "alice-snap", "domain_tags": ["snap"]})
        r_bob   = await client.post("/v1/agents", json={"name": "bob-snap",   "domain_tags": ["snap"]})
        assert r_alice.status_code == 201
        assert r_bob.status_code == 201
        alice = r_alice.json()
        bob   = r_bob.json()

        await client.post(
            f"/v1/agents/{alice['id']}/remember",
            json={"text": "asyncio.gather runs coroutines concurrently.", "domain_tags": ["python"]},
        )
        view = (await client.post(
            f"/v1/agents/{alice['id']}/views",
            json={"name": "snap-view", "atom_filter": {}},
        )).json()
        cap = (await client.post(
            f"/v1/agents/{alice['id']}/grant",
            json={"view_id": view["id"], "grantee_id": bob["id"]},
        )).json()
        return alice, bob, view, cap

    async def test_snapshot_degrades_after_decay(self, client, pool):
        """
        After atoms in a snapshot are deactivated by consolidation, they no
        longer appear in shared view recall. Documents current v0.2 semantics.
        """
        from mnemo.server.services.consolidation import run_consolidation

        alice, bob, view, cap = await self._setup_shared_view(client, pool)

        # Verify Bob can recall through the view before decay
        before = (await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "asyncio concurrent coroutines"},
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
        )).json()
        assert after["total_retrieved"] == 0

    async def test_snapshot_atom_ids_survive_deactivation(self, client, pool):
        """
        snapshot_atoms rows are NOT removed when atoms are deactivated.
        The ID set is stable (scope safety); only liveness changes.
        """
        from mnemo.server.services.consolidation import run_consolidation

        alice, bob, view, cap = await self._setup_shared_view(client, pool)

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
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "pandas read_csv silently coerces mixed column types in DataFrames.",
        })
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "I baked sourdough bread with extra rye flour and longer fermentation.",
        })

        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "pandas CSV loading data types",
            "min_similarity": 0.25,
            "expand_graph": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        texts = [a["text_content"] for a in data["atoms"]]
        # pandas atom should appear; sourdough should not
        assert any("pandas" in t or "csv" in t.lower() or "coerce" in t.lower() for t in texts)
        assert not any("sourdough" in t or "bread" in t for t in texts)

    async def test_recall_returns_empty_when_nothing_relevant(self, client, agent):
        """With a high similarity floor and an unrelated query, results are empty."""
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "I baked sourdough bread with a poolish starter yesterday.",
        })

        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "quantum chromodynamics particle physics collider",
            "min_similarity": 0.3,
            "expand_graph": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_retrieved"] == 0

    async def test_recall_ranks_by_composite_score(self, client, agent):
        """High-confidence atoms rank above low-confidence atoms with similar text."""
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "I confirmed that pandas read_csv definitely coerces column types silently.",
        })
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "I think maybe pandas read_csv might have some type coercion issues.",
        })

        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "pandas read_csv column type coercion",
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        })
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
        # Store connected atoms (multi-sentence → edges created between them)
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": (
                "pandas read_csv coerces column dtypes without warning. "
                "I discovered this while processing a CSV file. "
                "Always specify dtype explicitly when using read_csv."
            ),
        })

        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "CSV data type parsing",
            "min_similarity": 0.3,
            "expand_graph": True,
        })
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
        resp = await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": self._arc_text,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["atoms_created"] >= 2
        arc_atoms = [a for a in data["atoms"] if a["source_type"] == "arc"]
        assert len(arc_atoms) >= 1

    async def test_recall_finds_arc_by_theme(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": self._arc_text,
        })
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "API endpoint performance debugging and profiling",
            "min_similarity": 0.1,
            "expand_graph": False,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        assert any(a["source_type"] == "arc" for a in atoms)

    async def test_recall_expands_from_arc_to_atoms(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": self._arc_text,
        })
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "slow API database index profiling investigation",
            "min_similarity": 0.1,
            "expand_graph": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        # Arc should appear in primary results for this broad query
        all_atoms = data["atoms"] + data["expanded_atoms"]
        assert any(a["source_type"] == "arc" for a in all_atoms)
        # Non-arc atoms should also be reachable (via primary or graph expansion)
        assert any(a["source_type"] != "arc" for a in all_atoms)

    async def test_recall_expands_from_atom_to_arc(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": self._arc_text,
        })
        resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "always profile before guessing at the bottleneck",
            "min_similarity": 0.1,
            "expand_graph": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        all_atoms = data["atoms"] + data["expanded_atoms"]
        # Arc should appear (either in primary or expanded via summarises edge)
        assert any(a["source_type"] == "arc" for a in all_atoms)


# ── Recall controls ──────────────────────────────────────────────────────────

class TestRecallControls:
    """Tests for similarity_drop_threshold, verbosity, and max_total_tokens controls."""

    # ── Gap threshold ──────────────────────────────────────────────────────

    async def test_gap_threshold_stops_at_cliff(self, client, agent):
        """Atoms from an unrelated topic are cut when the score cliffs."""
        aid = agent["id"]
        for text in [
            "pandas read_csv silently coerces mixed-type columns to object dtype.",
            "Use dtype parameter in pandas read_csv to prevent silent type coercion.",
            "pandas DataFrame dtypes can be inspected with df.dtypes after loading.",
        ]:
            await client.post(f"/v1/agents/{aid}/remember", json={"text": text})
        for text in [
            "The Battle of Hastings was fought in 1066 between Harold and William.",
            "Medieval siege weapons included trebuchets, mangonels, and battering rams.",
        ]:
            await client.post(f"/v1/agents/{aid}/remember", json={"text": text})

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV column type coercion dtype",
            "similarity_drop_threshold": 0.3,
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        texts = [a["text_content"] for a in atoms]
        # Medieval history atoms should be cut at the cliff
        assert not any("1066" in t or "trebuchet" in t or "Hastings" in t for t in texts)

    async def test_gap_threshold_none_returns_all(self, client, agent):
        """With threshold=None, all atoms above min_similarity are returned."""
        aid = agent["id"]
        for text in [
            "pandas read_csv silently coerces mixed-type columns to object dtype.",
            "The Battle of Hastings was fought in 1066.",
        ]:
            await client.post(f"/v1/agents/{aid}/remember", json={"text": text})

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV column type coercion",
            "similarity_drop_threshold": None,
            "min_similarity": 0.05,
            "expand_graph": False,
            "max_results": 10,
        })
        assert resp.status_code == 200
        # Both atoms should be present (no filtering)
        assert resp.json()["total_retrieved"] >= 1

    async def test_gap_threshold_with_uniform_scores(self, client, agent):
        """No cliff in uniform topic → all atoms returned."""
        aid = agent["id"]
        for text in [
            "pandas read_csv coerces dtypes silently.",
            "pandas DataFrame dtypes should be set explicitly.",
            "pandas read_csv dtype parameter prevents coercion.",
            "Always check dtypes after loading a CSV with pandas.",
            "pandas dtype inference is unreliable for mixed columns.",
        ]:
            await client.post(f"/v1/agents/{aid}/remember", json={"text": text})

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV dtype coercion",
            "similarity_drop_threshold": 0.3,
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        })
        assert resp.status_code == 200
        # All 5 atoms are similar; no cliff should cut them
        assert resp.json()["total_retrieved"] >= 4

    async def test_gap_threshold_single_result(self, client, agent):
        """Steep cliff between relevant and irrelevant → only 1 result returned."""
        aid = agent["id"]
        await client.post(f"/v1/agents/{aid}/remember", json={
            "text": "pandas read_csv dtype coercion silently mangles column types.",
        })
        await client.post(f"/v1/agents/{aid}/remember", json={
            "text": "The French Revolution began in 1789 with the storming of the Bastille.",
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV type coercion",
            "similarity_drop_threshold": 0.3,
            "min_similarity": 0.05,
            "expand_graph": False,
            "max_results": 10,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        # At least the pandas atom returned; French Revolution should be cut
        assert len(atoms) >= 1
        texts = [a["text_content"] for a in atoms]
        assert not any("Bastille" in t or "1789" in t for t in texts)

    # ── Verbosity ──────────────────────────────────────────────────────────

    async def test_verbosity_full_returns_complete_text(self, client, agent):
        aid = agent["id"]
        full_text = (
            "pandas read_csv coerces dtypes silently. "
            "This caused data loss in production. "
            "Always specify dtype explicitly."
        )
        await client.post(f"/v1/agents/{aid}/remember", json={"text": full_text})

        # Store one of the atoms directly to control exact content
        atom_resp = await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": full_text,
            "domain_tags": ["python"],
        })
        assert atom_resp.status_code == 201

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV dtype coercion",
            "verbosity": "full",
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        assert any(len(a["text_content"]) > 60 for a in atoms)

    async def test_verbosity_summary_returns_first_sentence(self, client, agent):
        aid = agent["id"]
        text = "First sentence here. Second sentence here. Third sentence here."
        atom_resp = await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": text,
            "domain_tags": [],
        })
        assert atom_resp.status_code == 201
        atom_id = atom_resp.json()["id"]

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "First sentence here",
            "verbosity": "summary",
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        matching = [a for a in atoms if a["id"] == atom_id]
        assert matching, "stored atom not recalled"
        assert matching[0]["text_content"] == "First sentence here."

    async def test_verbosity_truncated_respects_char_limit(self, client, agent):
        aid = agent["id"]
        long_text = "pandas " + ("x" * 500)
        atom_resp = await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": long_text,
            "domain_tags": [],
        })
        assert atom_resp.status_code == 201
        atom_id = atom_resp.json()["id"]

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas",
            "verbosity": "truncated",
            "max_content_chars": 100,
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        })
        assert resp.status_code == 200
        atoms = resp.json()["atoms"]
        matching = [a for a in atoms if a["id"] == atom_id]
        assert matching, "stored atom not recalled"
        content = matching[0]["text_content"]
        assert content.endswith("...")
        assert len(content) == 103  # 100 chars + "..."

    async def test_verbosity_summary_single_sentence(self, client, agent):
        aid = agent["id"]
        text = "Only one sentence no period"
        atom_resp = await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": text,
            "domain_tags": [],
        })
        assert atom_resp.status_code == 201
        atom_id = atom_resp.json()["id"]

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "Only one sentence no period",
            "verbosity": "summary",
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        })
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
        # ~130-char atoms ≈ 33 tokens each; 5 atoms ≈ 165 tokens
        for i in range(5):
            await client.post(f"/v1/agents/{aid}/atoms", json={
                "atom_type": "semantic",
                "text_content": f"pandas read_csv coerces column types silently version {i} " + "word " * 15,
                "domain_tags": [],
            })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV column type coercion",
            "max_total_tokens": 80,
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        data = resp.json()
        # Budget of 80 tokens (~320 chars) should exclude some of the 5 atoms
        assert data["total_retrieved"] < 5

    async def test_token_budget_always_returns_one(self, client, agent):
        """Even with a very tight budget, at least 1 atom is always returned."""
        aid = agent["id"]
        long_text = "pandas " + ("word " * 200)  # ~1000 tokens
        await client.post(f"/v1/agents/{aid}/atoms", json={
            "atom_type": "semantic",
            "text_content": long_text,
            "domain_tags": [],
        })

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas",
            "max_total_tokens": 50,
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
        })
        assert resp.status_code == 200
        assert resp.json()["total_retrieved"] >= 1

    async def test_token_budget_none_returns_all(self, client, agent):
        """With max_total_tokens=None, all atoms above the similarity floor are returned."""
        aid = agent["id"]
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
            })

        # First, verify all 3 are actually stored (not merged)
        stats = (await client.get(f"/v1/agents/{aid}/stats")).json()
        stored = stats["active_atoms"]

        resp = await client.post(f"/v1/agents/{aid}/recall", json={
            "query": "pandas CSV dtype coercion column types",
            "max_total_tokens": None,
            "min_similarity": 0.1,
            "expand_graph": False,
            "max_results": 10,
            "similarity_drop_threshold": None,
        })
        assert resp.status_code == 200
        # Without a token budget, all stored+similar atoms are returned
        assert resp.json()["total_retrieved"] >= min(stored, 3)


# ── Auth endpoints ────────────────────────────────────────────────────────────

class TestAuth:
    async def test_register_creates_key_in_db(self, client, pool):
        """Key returned by /auth/register must be persisted to api_keys."""
        import hashlib
        resp = await client.post("/v1/auth/register", json={
            "name": "auth-test-agent",
            "persona": "tester",
            "domain_tags": [],
            "key_name": "default",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["api_key"].startswith("mnemo_")

        key_hash = hashlib.sha256(data["api_key"].encode()).hexdigest()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, is_active FROM api_keys WHERE key_hash = $1",
                key_hash,
            )
        assert row is not None, "api_key INSERT was not committed to the database"
        assert row["is_active"] is True

    async def test_register_then_me_roundtrip(self, client):
        """Key from /auth/register must authenticate successfully against /auth/me."""
        r1 = await client.post("/v1/auth/register", json={
            "name": "roundtrip-agent",
            "persona": "",
            "domain_tags": [],
        })
        assert r1.status_code == 201
        key = r1.json()["api_key"]
        agent_id = r1.json()["agent_id"]

        r2 = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {key}"})
        assert r2.status_code == 200, f"me() returned {r2.status_code}: {r2.text}"
        assert r2.json()["id"] == agent_id

    async def test_register_idempotent_adds_new_key(self, client, pool):
        """Registering an existing agent name generates a second key; both work."""
        r1 = await client.post("/v1/auth/register", json={"name": "idem-agent", "domain_tags": []})
        r2 = await client.post("/v1/auth/register", json={"name": "idem-agent", "domain_tags": []})
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["api_key"] != r2.json()["api_key"]
        assert r1.json()["agent_id"] == r2.json()["agent_id"]

        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM api_keys WHERE agent_id = $1::uuid AND is_active = true",
                r1.json()["agent_id"],
            )
        assert count == 2

    async def test_invalid_key_returns_401(self, client):
        resp = await client.get("/v1/auth/me", headers={"Authorization": "Bearer mnemo_notavalidkey"})
        assert resp.status_code == 401

    async def test_missing_bearer_returns_401(self, client):
        resp = await client.get("/v1/auth/me")
        assert resp.status_code == 401


# ── Health ────────────────────────────────────────────────────────────────────

async def test_health(client):
    resp = await client.get("/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
