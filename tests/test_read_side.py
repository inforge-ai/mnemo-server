"""v2 Phase 1 item 6: since/until/atom_types, recent, /agents/me, provenance."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from mnemo.server.decomposers import CandidateAtom, fake
from mnemo.server.embeddings import encode
from tests.test_consolidation import _insert_atom_sql

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _hdr(agent):
    return {"X-Agent-Key": agent["agent_key"]}


async def _seed(pool, agent_id, n=6, text="Clio checked the backup rotation log"):
    """n atoms with the same wording (high similarity), alternating types,
    remembered_on one day apart, created_at in the reverse order."""
    ids = []
    async with pool.acquire() as conn:
        for i in range(n):
            t = f"{text} entry {i}."
            atom_type = "episodic" if i % 2 == 0 else "semantic"
            aid = await _insert_atom_sql(conn, agent_id, atom_type, t, await encode(t), decay_type="none")
            await conn.execute(
                "UPDATE atoms SET remembered_on = $1, created_at = $2 WHERE id = $3",
                T0 + timedelta(days=i), T0 + timedelta(days=30 - i), aid,
            )
            ids.append(aid)
    return ids


async def _recall(client, agent, **body):
    payload = {"query": "Clio checked the backup rotation log", "expand_graph": False,
               "similarity_drop_threshold": None, "min_similarity": 0.0, **body}
    resp = await client.post(f"/v1/agents/{agent['id']}/recall", json=payload, headers=_hdr(agent))
    assert resp.status_code == 200, resp.text
    return resp.json()["atoms"]


async def test_recall_since_until_in_sql_before_limit(client, agent, pool):
    ids = await _seed(pool, UUID(agent["id"]), n=8)
    since, until = (T0 + timedelta(days=5)).isoformat(), (T0 + timedelta(days=7)).isoformat()
    atoms = await _recall(client, agent, since=since, until=until, max_results=1)
    # A post-filter on the top-2 over-fetch would usually return nothing; the
    # SQL filter guarantees an in-window match even with max_results=1.
    assert len(atoms) == 1
    assert UUID(atoms[0]["id"]) in {ids[5], ids[6]}
    atoms = await _recall(client, agent, since=since, until=until, max_results=10)
    assert {UUID(a["id"]) for a in atoms} == {ids[5], ids[6]}   # until exclusive (day 7 out)


async def test_recall_atom_types_filter(client, agent, pool):
    await _seed(pool, UUID(agent["id"]))
    atoms = await _recall(client, agent, atom_types=["semantic"], max_results=1)
    assert len(atoms) == 1 and atoms[0]["atom_type"] == "semantic"


async def test_recall_window_validation(client, agent):
    resp = await client.post(f"/v1/agents/{agent['id']}/recall", headers=_hdr(agent), json={
        "query": "x", "since": "2026-11-05T00:00:00Z", "until": "2026-11-01T00:00:00Z"})
    assert resp.status_code == 422


async def test_recent_orders_and_filters_without_access_update(client, agent, pool):
    ids = await _seed(pool, UUID(agent["id"]))
    url = f"/v1/agents/{agent['id']}/recent"

    stored = (await client.post(url, json={"limit": 3}, headers=_hdr(agent))).json()
    assert [UUID(a["id"]) for a in stored["atoms"]] == ids[:3]          # created_at DESC
    event = (await client.post(url, json={"limit": 3, "order_by": "event"}, headers=_hdr(agent))).json()
    assert [UUID(a["id"]) for a in event["atoms"]] == ids[::-1][:3]     # remembered_on DESC
    assert event["total_returned"] == 3

    filtered = (await client.post(url, headers=_hdr(agent), json={
        "atom_types": ["episodic"], "since": (T0 + timedelta(days=1)).isoformat(), "limit": 1})).json()
    assert [UUID(a["id"]) for a in filtered["atoms"]] == [ids[2]]       # filter before LIMIT

    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT max(access_count) FROM atoms WHERE agent_id = $1",
                                   UUID(agent["id"])) == 0


async def test_recent_hides_superseded_by_default(client, agent, pool):
    ids = await _seed(pool, UUID(agent["id"]), n=2)
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO edges (source_id, target_id, edge_type, weight) "
                           "VALUES ($1, $2, 'supersedes', 0.9)", ids[1], ids[0])
    url = f"/v1/agents/{agent['id']}/recent"
    shown = (await client.post(url, json={}, headers=_hdr(agent))).json()["atoms"]
    assert [UUID(a["id"]) for a in shown] == [ids[1]]
    allr = (await client.post(url, json={"include_superseded": True}, headers=_hdr(agent))).json()["atoms"]
    assert len(allr) == 2


async def test_recent_returns_superseded_when_setting_off(client, agent, pool, monkeypatch):
    from mnemo.server.config import settings
    monkeypatch.setattr(settings, "recall_hide_superseded", False)
    ids = await _seed(pool, UUID(agent["id"]), n=2)
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO edges (source_id, target_id, edge_type, weight) "
                           "VALUES ($1, $2, 'supersedes', 0.9)", ids[1], ids[0])
    url = f"/v1/agents/{agent['id']}/recent"
    shown = (await client.post(url, json={}, headers=_hdr(agent))).json()["atoms"]
    assert {UUID(a["id"]) for a in shown} == set(ids)


async def test_agents_me(client, agent, operator_with_key):
    resp = await client.get("/v1/agents/me", headers=_hdr(agent))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == agent["id"] and body["name"] == "test-agent" and body["status"] == "active"
    assert body["address"] == "test-agent:testop.testorg"
    _, _, op_headers = operator_with_key
    assert (await client.get("/v1/agents/me", headers=op_headers)).status_code == 403


async def test_paths_accept_bare_name_within_operator(client, agent, operator_with_key):
    resp = await client.post("/v1/agents/test-agent/recent", json={}, headers=_hdr(agent))
    assert resp.status_code == 200
    missing = await client.post("/v1/agents/no-such-agent/recent", json={}, headers=_hdr(agent))
    assert missing.status_code == 404
    assert "UUID, its name" in missing.json()["detail"]


async def test_recall_and_recent_carry_source_id_and_origin_class(client, agent, pool):
    text = "Clio checked the backup rotation log on Tuesday."
    fake.script([CandidateAtom(text=text, atom_type="episodic", confidence=0.9, source_quote=text)])
    stored = (await client.post(f"/v1/agents/{agent['id']}/remember", headers=_hdr(agent),
                                json={"text": text, "origin_class": "measurement"})).json()
    atoms = await _recall(client, agent)
    match = next(a for a in atoms if a["id"] == stored["atoms"][0]["id"])
    assert match["source_id"] == stored["source_id"] and match["origin_class"] == "measurement"
    rec = (await client.post(f"/v1/agents/{agent['id']}/recent", json={}, headers=_hdr(agent))).json()
    assert rec["atoms"][0]["origin_class"] == "measurement"
    got = (await client.get(f"/v1/agents/{agent['id']}/atoms/{match['id']}", headers=_hdr(agent))).json()
    assert got["source_id"] == stored["source_id"]
