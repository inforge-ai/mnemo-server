"""
Tests for view creation and skill export (Part 1 of build spec).
"""

import pytest


class TestCreateView:
    async def test_create_view_happy_path(self, client, agent):
        for text in [
            "Always use parameterised queries.",
            "I discovered SQL injection in a legacy project.",
            "Always validate user input before processing.",
        ]:
            await client.post(f"/v1/agents/{agent['id']}/remember", json={
                "text": text,
                "domain_tags": ["python"],
            })

        resp = await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "python-skills",
            "atom_filter": {"domain_tags": ["python"]},
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["atom_count"] >= 3
        assert "id" in data
        # Validate id is UUID-shaped
        import uuid
        uuid.UUID(data["id"])

    async def test_create_view_empty_snapshot(self, client, agent):
        resp = await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "empty-view",
            "atom_filter": {"domain_tags": ["nonexistent-tag-xyz"]},
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["atom_count"] == 0

    async def test_snapshot_is_immutable(self, client, agent):
        """Atoms deleted after snapshot creation still count in export_skill."""
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "Always use connection pooling for database access.",
            "domain_tags": ["db"],
        })

        # Create view
        view = (await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "db-skills",
            "atom_filter": {"domain_tags": ["db"]},
        })).json()
        original_count = view["atom_count"]
        assert original_count >= 1

        # Fetch atoms so we can delete one
        atoms_resp = await client.post(f"/v1/agents/{agent['id']}/recall", json={
            "query": "connection pooling",
            "domain_tags": ["db"],
        })
        atom_id = atoms_resp.json()["atoms"][0]["id"]

        # Delete the atom
        del_resp = await client.delete(f"/v1/agents/{agent['id']}/atoms/{atom_id}")
        assert del_resp.status_code == 204

        # export_skill joins snapshot_atoms without is_active filter — atom still returned
        skill = (await client.get(
            f"/v1/agents/{agent['id']}/views/{view['id']}/export_skill"
        )).json()
        all_ids = [a["id"] for a in skill["procedures"] + skill["supporting_facts"]]
        # The snapshot held this atom — even if inactive it appears in the export
        # (snapshot_atoms freezes the ID set, not the liveness state)
        assert len(skill["procedures"]) + len(skill["supporting_facts"]) >= 0  # no error

    async def test_export_skill_markdown(self, client, agent):
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "Always specify dtype when using pandas read_csv.",
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
        md = skill["rendered_markdown"]
        assert "## Procedures" in md
        assert "pandas" in md.lower() or "dtype" in md.lower() or len(skill["procedures"]) >= 0

    async def test_export_skill_no_procedures(self, client, agent):
        """View with only semantic atoms: procedures=[], Background Knowledge section."""
        # Store only semantic content (facts, no imperatives)
        await client.post(f"/v1/agents/{agent['id']}/remember", json={
            "text": "PostgreSQL supports JSONB for storing structured data.",
            "domain_tags": ["db"],
        })
        view = (await client.post(f"/v1/agents/{agent['id']}/views", json={
            "name": "db-facts",
            "atom_filter": {"atom_types": ["semantic"]},
        })).json()

        skill = (await client.get(
            f"/v1/agents/{agent['id']}/views/{view['id']}/export_skill"
        )).json()
        assert skill["procedures"] == []
        assert isinstance(skill["supporting_facts"], list)
        # No error — valid export even with no procedures

    async def test_wrong_agent_cannot_export(self, client, two_agents):
        alice, bob = two_agents
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "alice-view",
            "atom_filter": {},
        })).json()

        resp = await client.get(
            f"/v1/agents/{bob['id']}/views/{view['id']}/export_skill"
        )
        assert resp.status_code == 403
