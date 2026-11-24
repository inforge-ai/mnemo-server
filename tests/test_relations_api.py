"""v2 Phase 2 item 9: supersede, undo, conflicts, resolves, recall/recent output."""

import json
from datetime import datetime
from uuid import UUID

import pytest

from mnemo.server.config import settings
from mnemo.server.decisions.fake import FakeDecisions
from mnemo.server.decomposers import CandidateAtom, fake
from mnemo.server.services import relation_service
from tests.test_relations_store import script


def _hdr(agent):
    return {"X-Agent-Key": agent["agent_key"]}


def _atom(text, event_date=None):
    return CandidateAtom(text=text, atom_type="episodic", confidence=0.0, source_quote=text,
                         event_date=event_date, date_precision="day" if event_date else "unknown",
                         remembered_on=event_date, confidence_from_prior=True)


async def _store(client, agent, text, event_date=None, **body):
    fake.script([_atom(text, event_date)])
    r = await client.post(f"/v1/agents/{agent['id']}/remember", json={"text": text + ".", **body}, headers=_hdr(agent))
    assert r.status_code == 201, r.text
    return r.json()


async def _recall(client, agent, query, **body):
    r = await client.post(f"/v1/agents/{agent['id']}/recall",
                          json={"query": query, "min_similarity": 0.0, "similarity_drop_threshold": None, **body},
                          headers=_hdr(agent))
    assert r.status_code == 200, r.text
    return r.json()["atoms"]


async def _supersede(client, agent, atom_id, **body):
    return await client.post(f"/v1/agents/{agent['id']}/atoms/{atom_id}/supersede",
                             json={"reason": "moved", **body}, headers=_hdr(agent))


OLD = "The Gannet export runs at 03:00 UTC"
NEW = "The Gannet export runs at 04:00 UTC"


# ── supersede / undo ────────────────────────────────────────────────────────

async def test_supersede_by_atom_hides_and_reports_hidden_by(client, agent, pool):
    old = (await _store(client, agent, OLD))["atoms"][0]["id"]
    new = (await _store(client, agent, NEW))["atoms"][0]["id"]
    r = await _supersede(client, agent, old, by_atom_id=new)
    assert r.status_code == 201, r.text
    edge_id = r.json()["edge_id"]
    assert old not in {a["id"] for a in await _recall(client, agent, "Gannet export time")}
    shown = {a["id"]: a for a in await _recall(client, agent, "Gannet export time", include_superseded=True)}
    assert shown[old]["status"] == "superseded"
    assert shown[old]["hidden_by"] == {"relation": "replaces", "by_atom_id": new, "edge_id": edge_id}
    recent = (await client.post(f"/v1/agents/{agent['id']}/recent", json={}, headers=_hdr(agent))).json()["atoms"]
    assert old not in {a["id"] for a in recent}
    async with pool.acquire() as conn:
        e = await conn.fetchrow("SELECT decided_by, metadata FROM edges WHERE id = $1", UUID(edge_id))
    assert e["decided_by"] == "agent" and json.loads(e["metadata"])["reason"] == "moved"


async def test_supersede_errors(client, agent, two_agents):
    old = (await _store(client, agent, OLD))["atoms"][0]["id"]
    new = (await _store(client, agent, NEW))["atoms"][0]["id"]
    assert (await _supersede(client, agent, old)).status_code == 422                       # neither
    assert (await _supersede(client, agent, old, by_atom_id=new, text="both")).status_code == 422
    assert (await _supersede(client, agent, old, by_atom_id=old)).status_code == 422       # itself
    alice, _ = two_agents
    other = (await _store(client, alice, "Alice's own atom about exports"))["atoms"][0]["id"]
    assert (await _supersede(client, agent, other, by_atom_id=new)).status_code == 404     # not the owner
    assert (await _supersede(client, agent, old, by_atom_id=new)).status_code == 201
    assert (await _supersede(client, agent, old, by_atom_id=new)).status_code == 409       # already superseded


async def test_supersede_with_text_forces_the_target(client, agent, pool, monkeypatch):
    monkeypatch.setattr(settings, "relation_mode", "shadow")
    monkeypatch.setattr(relation_service, "get_decision_model", lambda: FakeDecisions(script))
    old = (await _store(client, agent, OLD))["atoms"][0]["id"]
    fake.script([_atom(NEW)])
    r = await _supersede(client, agent, old, text=NEW + ".")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["store_id"] and body["old_atom_id"] == old and body["by_atom_id"] != old
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM atoms WHERE id = $1", UUID(old)) == "superseded"
        # the relation step never decided against the forced target
        assert await conn.fetchval("SELECT count(*) FROM relation_decisions WHERE old_atom_id = $1", UUID(old)) == 0


async def test_undo_replaces_brings_the_atom_back(client, agent, pool):
    old = (await _store(client, agent, OLD))["atoms"][0]["id"]
    new = (await _store(client, agent, NEW))["atoms"][0]["id"]
    edge = (await _supersede(client, agent, old, by_atom_id=new)).json()["edge_id"]
    r = await client.post(f"/v1/edges/{edge}/undo", json={}, headers=_hdr(agent))
    assert r.status_code == 200, r.text
    assert r.json() == {"edge_id": edge, "undone": "replaces", "restored_atom_id": old}
    assert old in {a["id"] for a in await _recall(client, agent, "Gannet export time")}
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, valid_until FROM atoms WHERE id = $1", UUID(old))
        retired = await conn.fetchval("SELECT retired_at FROM edges WHERE id = $1", UUID(edge))
    assert row["status"] == "active" and row["valid_until"] is None and retired is not None
    assert (await client.post(f"/v1/edges/{edge}/undo", json={}, headers=_hdr(agent))).status_code == 409


async def test_undo_refuses_other_edges(client, agent, pool, two_agents):
    a = (await _store(client, agent, OLD))["atoms"][0]["id"]
    b = (await _store(client, agent, NEW))["atoms"][0]["id"]
    async with pool.acquire() as conn:
        legacy = await conn.fetchval("INSERT INTO edges (source_id, target_id, edge_type, weight, decided_by) "
                                     "VALUES ($1, $2, 'supersedes', 0.9, 'legacy_lifecycle') RETURNING id",
                                     UUID(b), UUID(a))
    assert (await client.post(f"/v1/edges/{legacy}/undo", json={}, headers=_hdr(agent))).status_code == 409
    alice, _ = two_agents
    assert (await client.post(f"/v1/edges/{legacy}/undo", json={}, headers=_hdr(alice))).status_code == 404


async def test_undo_same_restores_the_merged_atom_and_evidence(client, agent, pool, monkeypatch):
    monkeypatch.setattr(settings, "relation_mode", "enforce")
    monkeypatch.setattr(relation_service, "get_decision_model", lambda: FakeDecisions(script))
    d = datetime(2026, 11, 20)
    old = (await _store(client, agent, "Mira baseline is 52 [k:state]", d))["atoms"][0]["id"]
    async with pool.acquire() as conn:
        alpha0 = await conn.fetchval("SELECT confidence_alpha FROM atoms WHERE id = $1", UUID(old))
    new_atom = (await _store(client, agent, "Mira baseline is 52 [k:state] [r:same]", d))["atoms"][0]
    edge = new_atom["relations"][0]["edge_id"]
    r = await client.post(f"/v1/edges/{edge}/undo", json={}, headers=_hdr(agent))
    assert r.status_code == 200 and r.json()["restored_atom_id"] == new_atom["id"]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM atoms WHERE id = $1", UUID(new_atom["id"])) == "active"
        assert await conn.fetchval("SELECT confidence_alpha FROM atoms WHERE id = $1", UUID(old)) == alpha0


# ── conflicts ───────────────────────────────────────────────────────────────

async def _conflict(pool, agent, a, b):
    async with pool.acquire() as conn:
        return await conn.fetchval("INSERT INTO conflicts (agent_id, atom_a, atom_b) VALUES ($1, $2, $3) RETURNING id",
                                   UUID(agent["id"]), UUID(a), UUID(b))


async def test_conflict_partner_surfaces_once_in_recall(client, agent, pool):
    a = (await _store(client, agent, "The pump draws 40 W at idle"))["atoms"][0]["id"]
    b = (await _store(client, agent, "Unrelated note about vendor invoices and quarterly billing"))["atoms"][0]["id"]
    cid = await _conflict(pool, agent, a, b)
    atoms = await _recall(client, agent, "pump idle power draw", max_results=1)
    by_id = {x["id"]: x for x in atoms}
    assert by_id[a]["conflict_id"] == str(cid)
    assert by_id[b]["match_type"] == "conflict_partner" and by_id[b]["via"] == a
    assert [x["id"] for x in atoms].count(b) == 1


async def test_conflicts_listed_ranked_and_resolved_by_remember(client, agent, pool):
    a = (await _store(client, agent, "The pump draws 40 W at idle", origin_class="measurement"))["atoms"][0]["id"]
    b = (await _store(client, agent, "The pump draws 55 W at idle", origin_class="agent_inference"))["atoms"][0]["id"]
    c = (await _store(client, agent, "The heater draws 900 W"))["atoms"][0]["id"]
    d = (await _store(client, agent, "The heater draws 1200 W"))["atoms"][0]["id"]
    await _conflict(pool, agent, a, b)
    await _conflict(pool, agent, c, d)
    url = f"/v1/agents/{agent['id']}/conflicts"
    listed = (await client.get(url, headers=_hdr(agent))).json()["conflicts"]
    assert len(listed) == 2
    # agent_inference prior (3,2) is the lowest confidence → that conflict first; it also has differing origins
    assert {x["id"] for x in listed[0]["atoms"]} == {a, b} and listed[0]["origins_differ"] is True
    body = await _store(client, agent, "Measured again: the pump draws 41 W at idle", resolves=[a, b])
    assert body["warnings"] == []
    listed = (await client.get(url, headers=_hdr(agent))).json()["conflicts"]
    assert [{x["id"] for x in c_["atoms"]} for c_ in listed] == [{c, d}]
    closed = (await client.get(url + "?open=false", headers=_hdr(agent))).json()["conflicts"]
    done = next(x for x in closed if {y["id"] for y in x["atoms"]} == {a, b})
    assert done["resolved_by_atom_id"] == body["atoms"][0]["id"]
    async with pool.acquire() as conn:     # resolving supersedes neither side
        assert {r["status"] for r in await conn.fetch("SELECT status FROM atoms WHERE id = ANY($1)",
                                                      [UUID(a), UUID(b)])} == {"active"}


async def test_resolves_without_open_conflict_warns(client, agent):
    a = (await _store(client, agent, "The pump draws 40 W at idle"))["atoms"][0]["id"]
    b = (await _store(client, agent, "The pump draws 55 W at idle"))["atoms"][0]["id"]
    body = await _store(client, agent, "The pump draws 41 W at idle", resolves=[a, b])
    assert body["warnings"] == ["no open conflict between the atoms in `resolves`"]


async def test_recall_output_carries_kind_dates_status(client, agent):
    atom = (await _store(client, agent, OLD))["atoms"][0]
    got = next(a for a in await _recall(client, agent, "Gannet export") if a["id"] == atom["id"])
    assert got["kind"] == "unknown" and got["status"] == "active" and got["date_precision"] == "day"
    assert got["valid_from"] is not None
