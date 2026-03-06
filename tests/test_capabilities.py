"""
Tests for capability grant, revoke, and recall_shared (Part 2 of build spec).
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest


class TestCapabilities:
    async def _setup(self, client, alice, bob, domain="python"):
        """alice creates a view with memories and grants it to bob."""
        for text in [
            "Always use virtualenv for Python project isolation.",
            f"I discovered the importance of {domain} best practices.",
            f"Use linting tools to enforce {domain} code quality.",
        ]:
            await client.post(f"/v1/agents/{alice['id']}/remember", json={
                "text": text, "domain_tags": [domain],
            })
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": f"{domain}-skills",
            "atom_filter": {"domain_tags": [domain]},
        })).json()
        cap = (await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": bob["id"],
        })).json()
        return view, cap

    async def test_grant_happy_path(self, client, two_agents):
        alice, bob = two_agents
        view, cap = await self._setup(client, alice, bob)
        assert cap["grantee_id"] == bob["id"]
        assert cap["view_id"] == view["id"]
        assert cap["revoked"] is False

    async def test_grant_idempotent(self, client, two_agents):
        alice, bob = two_agents
        view, cap1 = await self._setup(client, alice, bob)

        # Grant again — should return same capability
        cap2 = (await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": bob["id"],
        })).json()
        assert cap2["id"] == cap1["id"]

        # Confirm only one row in DB via list_shared_views
        shared = (await client.get(f"/v1/agents/{bob['id']}/shared_views")).json()
        assert len(shared) == 1

    async def test_only_owner_can_grant(self, client, two_agents):
        alice, bob = two_agents
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "alice-private",
            "atom_filter": {},
        })).json()
        resp = await client.post(f"/v1/agents/{bob['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": alice["id"],
        })
        assert resp.status_code == 403

    async def test_recall_shared_happy_path(self, client, two_agents, pool):
        alice, bob = two_agents
        view, _cap = await self._setup(client, alice, bob)

        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "python project best practices"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_retrieved"] >= 1

        # Critical scope invariant: every returned atom_id must be in snapshot_atoms
        returned_ids = {a["id"] for a in data["atoms"] + data["expanded_atoms"]}
        async with pool.acquire() as conn:
            snap_ids = {
                str(r["atom_id"])
                for r in await conn.fetch(
                    "SELECT atom_id FROM snapshot_atoms WHERE view_id = $1",
                    view["id"],
                )
            }
        assert returned_ids.issubset(snap_ids), (
            f"Scope boundary breached. Atoms outside snapshot: {returned_ids - snap_ids}"
        )

    async def test_recall_shared_scope_boundary(self, client, two_agents):
        """Finance atoms must not appear in a python-scoped shared view."""
        alice, bob = two_agents

        # Alice stores python AND finance memories
        await client.post(f"/v1/agents/{alice['id']}/remember", json={
            "text": "Always use virtualenv for Python isolation.",
            "domain_tags": ["python"],
        })
        await client.post(f"/v1/agents/{alice['id']}/remember", json={
            "text": "Diversify bond portfolios to reduce interest rate risk.",
            "domain_tags": ["finance"],
        })

        # View is scoped to python only
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "python-only",
            "atom_filter": {"domain_tags": ["python"]},
        })).json()
        await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": bob["id"],
        })

        # Bob queries with a finance-related query
        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "bond portfolio interest rate"},
        )
        assert resp.status_code == 200
        data = resp.json()
        all_atoms = data["atoms"] + data["expanded_atoms"]
        for atom in all_atoms:
            assert "finance" not in atom.get("domain_tags", []), (
                f"Finance atom leaked into python-scoped view: {atom['text_content']}"
            )

    async def test_recall_shared_revoked_capability(self, client, two_agents):
        alice, bob = two_agents
        view, cap = await self._setup(client, alice, bob)

        revoke_resp = await client.post(f"/v1/capabilities/{cap['id']}/revoke")
        assert revoke_resp.status_code == 200

        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "anything"},
        )
        assert resp.status_code == 403

    async def test_recall_shared_expired_capability(self, client, two_agents):
        alice, bob = two_agents
        for text in ["Use linting to enforce code quality.", "I ran the linter today."]:
            await client.post(f"/v1/agents/{alice['id']}/remember", json={
                "text": text, "domain_tags": ["python"],
            })
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "short-lived",
            "atom_filter": {"domain_tags": ["python"]},
        })).json()

        # Grant with an expiry 1 second in the future
        expires = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": bob["id"],
            "expires_at": expires,
        })

        await asyncio.sleep(2)

        resp = await client.post(
            f"/v1/agents/{bob['id']}/shared_views/{view['id']}/recall",
            json={"query": "linting"},
        )
        assert resp.status_code == 403

    async def test_revoke_cascades(self, client, two_agents, pool):
        """Revoking parent cap also revokes child caps (via parent_cap_id FK)."""
        alice, bob = two_agents
        view, cap1 = await self._setup(client, alice, bob)

        # Insert a child capability at DB level (re-grant not exposed in client)
        async with pool.acquire() as conn:
            cap2_row = await conn.fetchrow(
                """
                INSERT INTO capabilities
                    (view_id, grantor_id, grantee_id, permissions, parent_cap_id)
                VALUES ($1, $2, $3, '{read}', $4)
                RETURNING id
                """,
                view["id"],
                bob["id"],
                alice["id"],
                cap1["id"],
            )
        cap2_id = cap2_row["id"]

        # Revoke the parent
        revoke_resp = await client.post(f"/v1/capabilities/{cap1['id']}/revoke")
        assert revoke_resp.json()["cascade_revoked"] >= 2

        # Child should also be revoked
        async with pool.acquire() as conn:
            child = await conn.fetchrow(
                "SELECT revoked FROM capabilities WHERE id = $1", cap2_id
            )
        assert child["revoked"] is True

    async def test_list_shared_views(self, client, pool):
        """Agent with two grants sees both; after one revoke, sees one."""
        clean = """
        DELETE FROM capabilities;
        DELETE FROM snapshot_atoms;
        DELETE FROM edges;
        DELETE FROM views;
        DELETE FROM atoms;
        DELETE FROM api_keys;
        DELETE FROM agents;
        """
        async with pool.acquire() as conn:
            await conn.execute(clean)

        alice_r = await client.post("/v1/agents", json={"name": "alice2", "domain_tags": []})
        bob_r = await client.post("/v1/agents", json={"name": "bob2", "domain_tags": []})
        carol_r = await client.post("/v1/agents", json={"name": "carol2", "domain_tags": []})
        alice = alice_r.json()
        bob = bob_r.json()
        carol = carol_r.json()

        # Alice and carol each grant a view to bob
        v1 = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "alice-view", "atom_filter": {},
        })).json()
        c1 = (await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": v1["id"], "grantee_id": bob["id"],
        })).json()

        v2 = (await client.post(f"/v1/agents/{carol['id']}/views", json={
            "name": "carol-view", "atom_filter": {},
        })).json()
        await client.post(f"/v1/agents/{carol['id']}/grant", json={
            "view_id": v2["id"], "grantee_id": bob["id"],
        })

        shared = (await client.get(f"/v1/agents/{bob['id']}/shared_views")).json()
        assert len(shared) == 2

        # Revoke alice's grant
        await client.post(f"/v1/capabilities/{c1['id']}/revoke")

        shared_after = (await client.get(f"/v1/agents/{bob['id']}/shared_views")).json()
        assert len(shared_after) == 1
        assert shared_after[0]["id"] == v2["id"]

    async def test_expires_at_in_past_rejected(self, client, two_agents):
        alice, bob = two_agents
        view = (await client.post(f"/v1/agents/{alice['id']}/views", json={
            "name": "test-view", "atom_filter": {},
        })).json()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        resp = await client.post(f"/v1/agents/{alice['id']}/grant", json={
            "view_id": view["id"],
            "grantee_id": bob["id"],
            "expires_at": past,
        })
        assert resp.status_code == 422
