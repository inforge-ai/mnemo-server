"""v2 Phase 1 items 3–4: sources, synchronous remember, failure, retry, status."""

import asyncio
import json
from uuid import UUID

import pytest

from mnemo.server.config import settings
from mnemo.server.decomposers import CandidateAtom, DecomposerError, fake
from tests.conftest import admin_headers

NOTE = "Clio moved the parser deploy to Thursday 2026-11-26. The VXUS sleeve is rebalanced monthly."


def _atom(text, quote, confidence=0.9, atom_type="episodic", subject="Clio"):
    return CandidateAtom(text=text, atom_type=atom_type, confidence=confidence,
                         source_quote=quote, subject=subject)


def _hdr(agent):
    return {"X-Agent-Key": agent["agent_key"]}


async def _remember(client, agent, text=NOTE, **body):
    return await client.post(f"/v1/agents/{agent['id']}/remember",
                             json={"text": text, **body}, headers=_hdr(agent))


# ── Synchronous remember ────────────────────────────────────────────────────

async def test_wait_returns_atoms_and_recall_finds_them(client, agent, pool):
    fake.script([
        _atom("Clio moved the parser deploy to Thursday 2026-11-26.",
              "Clio moved the parser deploy to Thursday 2026-11-26."),
    ])
    resp = await _remember(client, agent)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "complete"
    assert body["atoms_created"] == 1 and body["duplicates_merged"] == 0
    atom = body["atoms"][0]
    assert atom["merged_into_existing"] is False and atom["flags"] == []
    assert atom["source_quote"].startswith("Clio moved")

    recall = await client.post(f"/v1/agents/{agent['id']}/recall",
                               json={"query": "Clio moved the parser deploy to Thursday"}, headers=_hdr(agent))
    assert atom["id"] in {a["id"] for a in recall.json()["atoms"]}

    async with pool.acquire() as conn:
        src = await conn.fetchrow("SELECT * FROM sources WHERE id = $1", UUID(body["source_id"]))
        row = await conn.fetchrow("SELECT source_id, source_quote, verification, structured, decomposer_version "
                                  "FROM atoms WHERE id = $1", UUID(atom["id"]))
    assert src["text"] == NOTE and src["speaker"] == "test-agent" and src["origin_class"] == "unspecified"
    assert str(src["store_id"]) == body["store_id"]
    assert row["source_id"] == src["id"]
    assert json.loads(row["verification"])["flags"] == []
    assert json.loads(row["structured"])["subject"] == "Clio"
    assert row["decomposer_version"] == "fake:scripted:prompt_v2"


async def test_provenance_fields_stored_and_passed_to_decomposer(client, agent, pool):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE agents SET metadata = '{\"user_name\": \"Tom\"}' WHERE id = $1", UUID(agent["id"]))
    resp = await _remember(client, agent, speaker="Sam", addressee="Clio", channel="zulip #ops",
                           origin_class="user_stated", remembered_on="2026-11-20T00:00:00Z",
                           domain_tags=["ops"])
    assert resp.status_code == 201, resp.text
    req = fake.calls[-1]
    assert (req.speaker, req.addressee, req.channel, req.user_name, req.agent_name) == (
        "Sam", "Clio", "zulip #ops", "Tom", "test-agent")
    async with pool.acquire() as conn:
        src = await conn.fetchrow("SELECT * FROM sources WHERE id = $1", UUID(resp.json()["source_id"]))
    assert (src["speaker"], src["addressee"], src["channel"], src["origin_class"]) == (
        "Sam", "Clio", "zulip #ops", "user_stated")
    assert list(src["domain_tags"]) == ["ops"]


async def test_invalid_origin_class_rejected(client, agent):
    assert (await _remember(client, agent, origin_class="rumour")).status_code == 422


async def test_wait_false_queues(client, agent):
    resp = await _remember(client, agent, wait=False)
    assert resp.status_code == 201
    assert resp.json()["status"] == "queued"


async def test_wait_false_task_is_tracked(client, agent, pool, monkeypatch):
    from mnemo.server.services import store_service
    monkeypatch.setattr(settings, "sync_store_for_tests", False)
    resp = await _remember(client, agent, wait=False)
    tasks = store_service.background_tasks()
    assert tasks, "background store task must be referenced"
    await asyncio.gather(*tasks)
    status = await client.get(f"/v1/stores/{resp.json()['store_id']}/status", headers=_hdr(agent))
    assert status.json()["status"] == "complete"
    assert store_service.background_tasks() == set()


# ── No connection held during the model call or embedding ──────────────────

async def test_no_connection_held_during_decompose_and_embed(client, agent, pool, monkeypatch):
    seen = {}

    async def check(req):
        seen["decompose"] = (pool.get_idle_size(), pool.get_size())
        return [_atom("Clio moved the parser deploy.", "Clio moved the parser deploy")]

    from mnemo.server.services import atom_service
    real_batch = atom_service.encode_batch

    async def batch(texts, **kw):
        seen["embed"] = (pool.get_idle_size(), pool.get_size())
        return await real_batch(texts, **kw)

    monkeypatch.setattr(atom_service, "encode_batch", batch)
    fake.script(check)
    resp = await _remember(client, agent)
    assert resp.status_code == 201, resp.text
    for phase in ("decompose", "embed"):
        idle, size = seen[phase]
        assert idle == size, f"{phase}: {size - idle} connection(s) checked out"


# ── Failure, retry ──────────────────────────────────────────────────────────

async def test_decomposer_failure_is_502_and_keeps_source(client, agent, pool):
    fake.script(DecomposerError("provider timeout"))
    resp = await _remember(client, agent)
    assert resp.status_code == 502
    body = resp.json()
    assert body == {"status": "failed", "store_id": body["store_id"],
                    "source_id": body["source_id"], "error": "provider timeout"}
    store_id = UUID(body["store_id"])
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT text FROM sources WHERE store_id = $1", store_id) == NOTE
        assert await conn.fetchval("SELECT count(*) FROM atoms WHERE agent_id = $1", UUID(agent["id"])) == 0
        job = await conn.fetchrow("SELECT status, error FROM store_jobs WHERE store_id = $1", store_id)
        failure = await conn.fetchrow("SELECT original_text, error FROM store_failures WHERE id = $1", store_id)
    assert (job["status"], job["error"]) == ("failed", "provider timeout")
    assert failure["original_text"] is None and "DecomposerError" in failure["error"]

    # Retry re-reads the source and succeeds with the same ids.
    fake.script([_atom("Clio moved the parser deploy to Thursday 2026-11-26.",
                       "Clio moved the parser deploy to Thursday 2026-11-26.")])
    retry = await client.post(f"/v1/stores/{store_id}/retry", headers=_hdr(agent))
    assert retry.status_code == 201, retry.text
    assert retry.json()["store_id"] == str(store_id)
    assert retry.json()["source_id"] == body["source_id"]
    assert fake.calls[-1].text == NOTE

    status = (await client.get(f"/v1/stores/{store_id}/status", headers=_hdr(agent))).json()
    assert status["status"] == "complete" and status["error"] is None
    assert [a["text_content"] for a in status["atoms"]] == ["Clio moved the parser deploy to Thursday 2026-11-26."]

    again = await client.post(f"/v1/stores/{store_id}/retry", headers=_hdr(agent))
    assert again.status_code == 409


async def test_persist_failure_rolls_back_every_atom(client, agent, pool, monkeypatch):
    from mnemo.server.services import atom_service
    real = atom_service._create_similarity_edges

    async def boom(*a, **kw):
        raise RuntimeError("db exploded")

    fake.script([_atom("Atom one about Clio.", "Clio"), _atom("Atom two about Clio.", "Clio")])
    monkeypatch.setattr(atom_service, "_create_similarity_edges", boom)
    resp = await _remember(client, agent)
    monkeypatch.setattr(atom_service, "_create_similarity_edges", real)
    assert resp.status_code == 500
    assert resp.json()["error"] == "storage error"
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM atoms WHERE agent_id = $1", UUID(agent["id"])) == 0


# ── Status: auth and merged atoms ───────────────────────────────────────────

async def test_status_visibility(client, operator_with_key, agent, pool):
    _, op_key, op_headers = operator_with_key
    resp = await _remember(client, agent)
    store_id = resp.json()["store_id"]
    url = f"/v1/stores/{store_id}/status"

    other_agent = (await client.post("/v1/agents", json={"name": "other-agent"}, headers=op_headers)).json()
    other_op = (await client.post("/v1/admin/operators", headers=admin_headers(), json={
        "username": "otherop", "org": "otherorg", "display_name": "Other", "email": "o@test.com",
    })).json()

    assert (await client.get(url, headers=_hdr(agent))).status_code == 200
    assert (await client.get(url, headers=op_headers)).status_code == 200
    assert (await client.get(url, headers=admin_headers())).status_code == 200
    assert (await client.get(url, headers=_hdr(other_agent))).status_code == 404
    assert (await client.get(url, headers={"X-Operator-Key": other_op["api_key"]})).status_code == 404
    assert (await client.post(f"/v1/stores/{store_id}/retry", headers=_hdr(other_agent))).status_code == 404


async def test_merged_atoms_recorded_in_source_merges_and_status(client, agent, pool):
    text = "Clio rebalances the VXUS sleeve monthly."
    fake.script([_atom(text, text, atom_type="semantic")], [_atom(text, text, atom_type="semantic")])
    first = (await _remember(client, agent, text=text)).json()
    second = (await _remember(client, agent, text=text)).json()
    assert second["duplicates_merged"] == 1 and second["atoms_created"] == 0
    merged = second["atoms"][0]
    assert merged["merged_into_existing"] is True
    assert merged["id"] == first["atoms"][0]["id"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT atom_id, source_quote FROM source_merges WHERE source_id = $1",
                                  UUID(second["source_id"]))
    assert str(row["atom_id"]) == merged["id"] and row["source_quote"] == text
    status = (await client.get(f"/v1/stores/{second['store_id']}/status", headers=_hdr(agent))).json()
    assert [(a["id"], a["merged_into_existing"]) for a in status["atoms"]] == [(merged["id"], True)]


# ── Checks: only ungrounded_quote lowers the band ───────────────────────────

async def test_only_ungrounded_quote_lowers_band(client, agent, pool):
    note = "Tom said the VXUS position is down 3% since the review."
    fake.script([
        _atom("Tom said the VXUS position is down 3%.", "the VXUS position is down 3%"),              # clean
        _atom("Tom said the Vanguard fund is down 3%.", "the VXUS position is down 3%"),              # abbreviation_altered
        _atom("Tom said the VTIAX position is down.", "the VXUS position is down 3%"),                # altered + unsupported
        _atom("Tom said it fell sharply.", "it fell sharply after lunch"),                            # ungrounded
    ])
    body = (await _remember(client, agent, text=note)).json()
    flags = [a["flags"] for a in body["atoms"]]
    assert flags == [[], ["abbreviation_altered"], ["abbreviation_altered", "unsupported_term"], ["ungrounded_quote"]]
    async with pool.acquire() as conn:
        rows = [await conn.fetchrow("SELECT confidence_alpha, confidence_beta, text_content, verification "
                                    "FROM atoms WHERE id = $1", UUID(a["id"])) for a in body["atoms"]]
    bands = [(r["confidence_alpha"], r["confidence_beta"]) for r in rows]
    assert bands == [(8.0, 1.0), (8.0, 1.0), (8.0, 1.0), (4.0, 2.0)]
    # Record-only: text stored exactly as decomposed, nothing interpreted.
    assert rows[1]["text_content"] == "Tom said the Vanguard fund is down 3%."
    assert json.loads(rows[3]["verification"])["band_downgraded"] is True
    assert json.loads(rows[1]["verification"])["band_downgraded"] is False


# ── Startup sweep ───────────────────────────────────────────────────────────

async def test_sweep_marks_stuck_jobs_failed(client, agent, pool):
    from mnemo.server.services.store_service import sweep_stuck_jobs
    async with pool.acquire() as conn:
        op = await conn.fetchval("SELECT operator_id FROM agents WHERE id = $1", UUID(agent["id"]))
        old = await conn.fetchval(
            "INSERT INTO store_jobs (store_id, agent_id, operator_id, status, created_at) "
            "VALUES (gen_random_uuid(), $1, $2, 'decomposing', now() - interval '11 minutes') RETURNING store_id",
            UUID(agent["id"]), op)
        fresh = await conn.fetchval(
            "INSERT INTO store_jobs (store_id, agent_id, operator_id, status) "
            "VALUES (gen_random_uuid(), $1, $2, 'pending') RETURNING store_id", UUID(agent["id"]), op)
    assert await sweep_stuck_jobs(pool) == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM store_jobs WHERE store_id = $1", old) == "failed"
        assert await conn.fetchval("SELECT status FROM store_jobs WHERE store_id = $1", fresh) == "pending"


async def test_run_store_reports_timings(client, agent, pool):
    from mnemo.server.services import store_service
    async with pool.acquire() as conn:
        op = await conn.fetchval("SELECT operator_id FROM agents WHERE id = $1", UUID(agent["id"]))
        src = await store_service.create_store(
            conn, agent_id=UUID(agent["id"]), operator_id=op, agent_name="test-agent", agent_metadata={},
            text=NOTE, domain_tags=[], remembered_on=None, speaker=None, addressee=None, channel=None,
            origin_class="unspecified",
        )
    outcome = await store_service.run_store(pool, src)
    assert outcome["status"] == "complete"
    assert set(outcome["timings_ms"]) == {"decompose", "embed", "persist"}
    assert all(v >= 0 for v in outcome["timings_ms"].values())
