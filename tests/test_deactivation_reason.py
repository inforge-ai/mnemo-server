"""v2 Phase 0: every code path that deactivates an atom records why."""

from uuid import UUID

import pytest

from mnemo.server.config import settings
from mnemo.server.embeddings import encode
from mnemo.server.services.consolidation import run_consolidation
from tests.test_consolidation import _insert_atom_sql


async def _reason(pool, atom_id):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_active, deactivation_reason FROM atoms WHERE id = $1", atom_id,
        )
    return row["is_active"], row["deactivation_reason"]


async def test_delete_endpoint_sets_deleted_by_agent(client, agent, pool):
    agent_id = UUID(agent["id"])
    text = "The release checklist lives in the ops handbook."
    async with pool.acquire() as conn:
        atom_id = await _insert_atom_sql(conn, agent_id, "semantic", text, await encode(text), decay_type="none")

    resp = await client.delete(
        f"/v1/agents/{agent_id}/atoms/{atom_id}",
        headers={"X-Agent-Key": agent["agent_key"]},
    )
    assert resp.status_code == 204, resp.text
    assert await _reason(pool, atom_id) == (False, "deleted_by_agent")


async def test_decay_sets_decay(agent, pool, monkeypatch):
    monkeypatch.setattr(settings, "consolidation_decay_enabled", True)
    agent_id = UUID(agent["id"])
    text = "The staging cluster was rebooted after the kernel patch."
    async with pool.acquire() as conn:
        atom_id = await _insert_atom_sql(conn, agent_id, "episodic", text, await encode(text), half_life=1.0)
        await conn.execute(
            "UPDATE atoms SET created_at = now() - interval '1 year', last_accessed = NULL, "
            "access_count = 0 WHERE id = $1",
            atom_id,
        )

    result = await run_consolidation(pool)
    assert result["decayed"] >= 1
    assert await _reason(pool, atom_id) == (False, "decay")


async def test_merge_sets_merged(agent, pool, monkeypatch):
    monkeypatch.setattr(settings, "consolidation_merge_enabled", True)
    agent_id = UUID(agent["id"])
    emb = await encode("Deploys to production happen on Tuesdays and Thursdays.")
    async with pool.acquire() as conn:
        older = await _insert_atom_sql(conn, agent_id, "semantic", "Deploys Tue/Thu.", emb, decay_type="none")
        await conn.execute("UPDATE atoms SET created_at = now() - interval '1 day' WHERE id = $1", older)
        newer = await _insert_atom_sql(conn, agent_id, "semantic", "Deploys Tue+Thu.", emb, decay_type="none")

    result = await run_consolidation(pool)
    assert result["merged"] >= 1
    assert await _reason(pool, newer) == (False, "merged")
    assert await _reason(pool, older) == (True, None)
