"""v2 Phase 2: relations on the store path (shadow / enforce / outage / pending).

A scripted FakeDecisions reads tags in the synthetic atom text:
  [k:<kind>]       the kind question answers <kind> with p 0.95 (no tag: p 0.5 → unknown)
  [r:<relation>]   on the NEW atom: the relation question answers it with p 0.95
  [c] [a] [x]      on the NEW atom: change / same_attribute / correction answer yes (0.9)
  [g0]             the grounding question answers 0.01 (below the model cut-off)
"""

import asyncio
import json
import re
from datetime import datetime
from uuid import UUID

import pytest

from mnemo.server.config import settings
from mnemo.server.decisions.fake import FakeDecisions
from mnemo.server.decomposers import CandidateAtom, fake
from mnemo.server.services import relation_service

RELS = ["same", "replaces", "refines", "conflict", "unrelated"]


def _section(state: str, header: str) -> str:
    m = re.search(rf"{header}[^\n]*\n(.*?)(\n\n|\nEVENT DATE|\Z)", state, re.S)
    return m.group(1) if m else ""


def script(state, questions):
    out = {}
    if "kind" in questions:
        text = _section(state, "ATOM:")
        m = re.search(r"\[k:(\w+)\]", text)
        keys = list(questions["kind"]["criteria"])
        if m:
            probs = {k: (0.95 if k == m.group(1) else 0.05 / 3) for k in keys}
        else:
            probs = {k: (0.5 if k == keys[0] else 0.5 / 3) for k in keys}
        out["kind"] = {"type": "choice", "choice": max(probs, key=probs.get), "probabilities": probs}
    if "grounded" in questions:
        out["grounded"] = {"type": "noul", "noul": 0.01 if "[g0]" in state else 0.9}
    if "time_unresolved" in questions:
        out["time_unresolved"] = {"type": "noul", "noul": 0.1}
    if "relation" in questions:
        new = _section(state, "NEW ATOM")
        m = re.search(r"\[r:(\w+)\]", new)
        rel = m.group(1) if m else "unrelated"
        probs = {k: (0.95 if k == rel else 0.05 / 4) for k in RELS}
        out["relation"] = {"type": "choice", "choice": rel, "probabilities": probs}
        out["change"] = {"type": "noul", "noul": 0.9 if "[c]" in new else 0.1}
        out["same_attribute"] = {"type": "noul", "noul": 0.9 if "[a]" in new else 0.1}
        out["correction"] = {"type": "noul", "noul": 0.9 if "[x]" in new else 0.1}
    return out


@pytest.fixture
def decisions(monkeypatch):
    model = FakeDecisions(script)
    monkeypatch.setattr(relation_service, "get_decision_model", lambda: model)
    return model


@pytest.fixture
def mode(monkeypatch):
    def set_mode(m):
        monkeypatch.setattr(settings, "relation_mode", m)
    return set_mode


def _atom(text, event_date=None, precision="unknown", atom_type="episodic"):
    return CandidateAtom(text=text, atom_type=atom_type, confidence=0.0, source_quote=text,
                         event_date=event_date, date_precision=precision if event_date else "unknown",
                         remembered_on=event_date, confidence_from_prior=True)


def _hdr(agent):
    return {"X-Agent-Key": agent["agent_key"]}


async def _remember(client, agent, atoms, text=None, **body):
    """The note contains every atom's text, so the deterministic quote check passes."""
    text = text or " ".join(a.text + "." for a in atoms)
    fake.script(atoms)
    resp = await client.post(f"/v1/agents/{agent['id']}/remember", json={"text": text, **body}, headers=_hdr(agent))
    assert resp.status_code == 201, resp.text
    return resp.json()


D1, D2 = datetime(2026, 11, 20), datetime(2026, 11, 23)
OLD = "Mira resting heart rate baseline is 52 [k:state]"
NEW = "Mira resting heart rate baseline is 54 [k:state] [r:replaces] [a]"


async def _rows(pool, sql, *args):
    async with pool.acquire() as conn:
        return await conn.fetch(sql, *args)


# ── off ─────────────────────────────────────────────────────────────────────

async def test_off_writes_dates_but_no_decisions(client, agent, pool, mode):
    mode("off")
    body = await _remember(client, agent, [_atom("Undated statement about Mira [k:state]")])
    a = body["atoms"][0]
    assert a["relations"] == [] and body["relations_pending"] == []
    row = (await _rows(pool, "SELECT kind, valid_from, created_at, date_precision, date_basis FROM atoms WHERE id = $1",
                       UUID(a["id"])))[0]
    assert row["kind"] == "unknown" and row["date_basis"] == "stated" and row["date_precision"] == "day"
    assert row["valid_from"] == row["created_at"]
    assert not await _rows(pool, "SELECT 1 FROM relation_decisions")


async def test_v3_atom_uses_origin_prior(client, agent, pool, mode):
    mode("off")
    body = await _remember(client, agent, [_atom("A measured fact [k:state]")], origin_class="measurement")
    row = (await _rows(pool, "SELECT confidence_alpha, confidence_beta FROM atoms WHERE id = $1",
                       UUID(body["atoms"][0]["id"])))[0]
    assert (row["confidence_alpha"], row["confidence_beta"]) == (8.0, 1.0)


# ── shadow ──────────────────────────────────────────────────────────────────

async def test_shadow_records_only(client, agent, pool, mode, decisions):
    mode("shadow")
    old = (await _remember(client, agent, [_atom(OLD, D1, "day")]))["atoms"][0]
    body = await _remember(client, agent, [_atom(NEW, D2, "day")])
    new = body["atoms"][0]
    assert new["kind"] == "state" and new["kind_p"] == pytest.approx(0.95)
    rels = new["relations"]
    assert [(r["relation"], r["target_atom_id"], r["mode"], r["edge_id"]) for r in rels] == \
        [("replaces", old["id"], "shadow", None)]
    d = (await _rows(pool, "SELECT * FROM relation_decisions WHERE new_atom_id = $1", UUID(new["id"])))[0]
    assert d["mode"] == "shadow" and d["final_relation"] == "replaces" and d["raw_choice"] == "replaces"
    assert set(json.loads(d["probabilities"])) == {"relation", "change", "same_attribute", "correction"}
    inputs = json.loads(d["inputs"])
    assert inputs["new"]["kind"] == "state" and inputs["old"]["basis"] == "event" and inputs["questions"] == "q1"
    # nothing applied
    status = await _rows(pool, "SELECT status FROM atoms WHERE id = $1", UUID(old["id"]))
    assert status[0]["status"] == "active"
    assert not await _rows(pool, "SELECT 1 FROM edges WHERE decided_by = 'relation_v2'")


async def test_shadow_lazy_kind_stays_in_inputs(client, agent, pool, mode, decisions):
    mode("off")
    old = (await _remember(client, agent, [_atom(OLD, D1, "day")]))["atoms"][0]          # stored kind unknown
    mode("shadow")
    await _remember(client, agent, [_atom(NEW, D2, "day")])
    d = (await _rows(pool, "SELECT inputs FROM relation_decisions WHERE old_atom_id = $1", UUID(old["id"])))[0]
    inputs = json.loads(d["inputs"])
    assert inputs["old_kind_lazy"] is True and inputs["old"]["kind"] == "state" and inputs["old_kind_stored"] == "unknown"
    assert (await _rows(pool, "SELECT kind FROM atoms WHERE id = $1", UUID(old["id"])))[0]["kind"] == "unknown"


async def test_shadow_keeps_legacy_dedup(client, agent, pool, mode, decisions):
    mode("shadow")
    await _remember(client, agent, [_atom(OLD, D1, "day")])
    body = await _remember(client, agent, [_atom(OLD, D1, "day")])
    assert body["duplicates_merged"] == 1
    rows = await _rows(pool, "SELECT new_atom_id, inputs FROM relation_decisions")
    assert rows and all(r["new_atom_id"] is None and "dedup_target" in json.loads(r["inputs"]) for r in rows)


# ── enforce ─────────────────────────────────────────────────────────────────

async def test_enforce_replaces(client, agent, pool, mode, decisions):
    mode("enforce")
    old = (await _remember(client, agent, [_atom(OLD, D1, "day")]))["atoms"][0]
    new = (await _remember(client, agent, [_atom(NEW, D2, "day")]))["atoms"][0]
    r = new["relations"][0]
    assert r["relation"] == "replaces" and r["edge_id"] and r["mode"] == "enforce"
    o = (await _rows(pool, "SELECT status, valid_until FROM atoms WHERE id = $1", UUID(old["id"])))[0]
    assert o["status"] == "superseded" and o["valid_until"].date() == D2.date()
    e = (await _rows(pool, "SELECT edge_type, decided_by, source_id, target_id FROM edges WHERE id = $1",
                     UUID(r["edge_id"])))[0]
    assert (e["edge_type"], e["decided_by"], str(e["source_id"]), str(e["target_id"])) == \
        ("replaces", "relation_v2", new["id"], old["id"])


async def test_enforce_older_over_newer_is_review(client, agent, pool, mode, decisions):
    mode("enforce")
    old = (await _remember(client, agent, [_atom(OLD, D2, "day")]))["atoms"][0]
    new = (await _remember(client, agent, [_atom(NEW, D1, "day")]))["atoms"][0]
    assert [x["relation"] for x in new["relations"]] == ["review"]
    d = (await _rows(pool, "SELECT guard_fired FROM relation_decisions WHERE new_atom_id = $1", UUID(new["id"])))[0]
    assert d["guard_fired"] == "date_order"
    assert (await _rows(pool, "SELECT status FROM atoms WHERE id = $1", UUID(old["id"])))[0]["status"] == "active"


async def test_enforce_same_merges_and_adds_evidence(client, agent, pool, mode, decisions):
    mode("enforce")
    old = (await _remember(client, agent, [_atom(OLD, D1, "day")]))["atoms"][0]
    before = (await _rows(pool, "SELECT confidence_alpha FROM atoms WHERE id = $1", UUID(old["id"])))[0]
    new = (await _remember(client, agent, [_atom("Mira baseline heart rate is 52 [k:state] [r:same]", D1, "day")]))["atoms"][0]
    assert new["relations"][0]["relation"] == "same" and new["status"] == "merged"
    st = (await _rows(pool, "SELECT status FROM atoms WHERE id = $1", UUID(new["id"])))[0]["status"]
    after = (await _rows(pool, "SELECT confidence_alpha FROM atoms WHERE id = $1", UUID(old["id"])))[0]
    assert st == "merged" and after["confidence_alpha"] > before["confidence_alpha"]
    meta = json.loads((await _rows(pool, "SELECT metadata FROM edges WHERE edge_type = 'same'"))[0]["metadata"])
    assert meta["evidence_added"] is True and meta["old_alpha"] == before["confidence_alpha"]


async def test_enforce_conflict_opens_one_conflict(client, agent, pool, mode, decisions):
    mode("enforce")
    old = (await _remember(client, agent, [_atom("Mira's pump uses 40 W [k:state]", D1, "day")]))["atoms"][0]
    new = (await _remember(client, agent, [_atom("Mira's pump uses 55 W [k:state] [r:conflict]", D1, "day")]))["atoms"][0]
    assert new["relations"][0]["relation"] == "conflict"
    rows = await _rows(pool, "SELECT atom_a, atom_b, resolved_at FROM conflicts")
    assert len(rows) == 1 and {str(rows[0]["atom_a"]), str(rows[0]["atom_b"])} == {old["id"], new["id"]}


async def test_enforce_dedup_off(client, agent, pool, mode, decisions):
    mode("enforce")
    await _remember(client, agent, [_atom(OLD, D1, "day")])
    body = await _remember(client, agent, [_atom(OLD, D1, "day")])
    assert body["duplicates_merged"] == 0 and body["atoms_created"] == 1


async def test_enforce_persists_lazy_kind(client, agent, pool, mode, decisions):
    mode("off")
    old = (await _remember(client, agent, [_atom(OLD, D1, "day")]))["atoms"][0]
    mode("enforce")
    await _remember(client, agent, [_atom(NEW, D2, "day")])
    assert (await _rows(pool, "SELECT kind FROM atoms WHERE id = $1", UUID(old["id"])))[0]["kind"] == "state"


async def test_model_ungrounded_scales_alpha_and_flags(client, agent, pool, mode, decisions):
    mode("shadow")
    body = await _remember(client, agent, [_atom("A doubtful claim [k:rule] [g0]")])
    a = body["atoms"][0]
    assert "model_ungrounded" in a["flags"]
    row = (await _rows(pool, "SELECT confidence_alpha, verification FROM atoms WHERE id = $1", UUID(a["id"])))[0]
    assert row["confidence_alpha"] == pytest.approx(2.5)          # unspecified prior 4 → 1 + 3 * 0.5
    assert json.loads(row["verification"])["grounded_p"] == pytest.approx(0.01)


# ── outage, budget, pending ─────────────────────────────────────────────────

async def test_outage_stores_everything_pending(client, agent, pool, mode, monkeypatch):
    mode("enforce")
    await _remember(client, agent, [_atom(OLD, D1, "day")])

    def down(state, questions):
        raise RuntimeError("provider down")
    monkeypatch.setattr(relation_service, "get_decision_model", lambda: FakeDecisions(down))
    body = await _remember(client, agent, [_atom(NEW, D2, "day")])
    new = body["atoms"][0]
    assert body["atoms_created"] == 1 and new["kind"] == "unknown"
    assert body["relations_pending"] == [new["id"]]
    rows = await _rows(pool, "SELECT final_relation FROM relation_decisions")
    assert [r["final_relation"] for r in rows] == ["pending"]

    # the worker decides it once the model is back (backoff skipped by ageing the row)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE relation_decisions SET created_at = now() - interval '1 hour'")
    counts = await relation_service.process_pending(pool, FakeDecisions(script))
    assert counts == {"picked": 1, "decided": 1, "still_pending": 0}
    d = (await _rows(pool, "SELECT final_relation, attempts, guard_fired FROM relation_decisions"))[0]
    # the new atom's kind stayed `unknown` (classification failed), so the matrix allows only refines
    assert d["final_relation"] in ("unrelated", "refines") and d["attempts"] == 2


async def test_budget_exhausted_goes_pending(client, agent, pool, mode, monkeypatch):
    mode("shadow")
    monkeypatch.setattr(settings, "relation_budget_seconds", 0.05)
    await _remember(client, agent, [_atom(OLD, D1, "day")])

    class Slow(FakeDecisions):
        async def ask(self, state, questions, *, deadline=None):
            await asyncio.sleep(0.2)
            return await super().ask(state, questions, deadline=deadline)
    monkeypatch.setattr(relation_service, "get_decision_model", lambda: Slow(script))
    body = await _remember(client, agent, [_atom(NEW, D2, "day")])
    assert body["relations_pending"] == [body["atoms"][0]["id"]]


async def test_no_connection_held_during_model_calls(client, agent, pool, mode, monkeypatch):
    mode("shadow")
    await _remember(client, agent, [_atom(OLD, D1, "day")])
    seen = []

    def watching(state, questions):
        seen.append(pool.get_size() - pool.get_idle_size())
        return script(state, questions)
    monkeypatch.setattr(relation_service, "get_decision_model", lambda: FakeDecisions(watching))
    await _remember(client, agent, [_atom(NEW, D2, "day")])
    assert seen and max(seen) == 0, seen


async def test_relation_step_crash_fails_open(client, agent, pool, mode, monkeypatch):
    mode("enforce")

    async def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(relation_service, "prepare_relations", boom)
    body = await _remember(client, agent, [_atom(OLD, D1, "day")])
    assert body["atoms_created"] == 1 and body["relations_pending"] == []


async def test_same_from_the_same_source_adds_no_evidence(client, agent, pool):
    """Spec fixture 'duplicate from the same source': `same`, no evidence added.
    Across remember calls sources are always independent, so this is tested at apply()."""
    from mnemo.server.services.relations import Decision
    from mnemo.server.services.relation_service import Candidate, PairResult, apply
    async with pool.acquire() as conn:
        src = await conn.fetchval(
            "INSERT INTO sources (agent_id, store_id, text, speaker) VALUES ($1, gen_random_uuid(), 'n', 's') "
            "RETURNING id", UUID(agent["id"]))
        store = await conn.fetchval("SELECT store_id FROM sources WHERE id = $1", src)
        from mnemo.server.embeddings import encode
        from tests.test_consolidation import _insert_atom_sql
        old = await _insert_atom_sql(conn, UUID(agent["id"]), "semantic", "dup fact", await encode("dup fact"))
        new = await _insert_atom_sql(conn, UUID(agent["id"]), "semantic", "dup fact again", await encode("dup fact"))
        alpha0 = await conn.fetchval("SELECT confidence_alpha FROM atoms WHERE id = $1", old)
        cand = Candidate(id=old, text="dup fact", kind="rule", kind_p=0.9, valid_from=D1, precision="day",
                         basis="event", origin_class="unspecified", time_unresolved=False, time_known=True,
                         source_id=src, store_id=store, alpha=alpha0, beta=2.0, cosine=1.0)
        pr = PairResult(0, cand, None, Decision("same", None, "same"), {},
                        {"relation": {"same": 0.95}, "change": 0, "same_attribute": 0, "correction": 0})
        async with conn.transaction():
            final, guard, edge_id = await apply(conn, UUID(agent["id"]), new, src, store, (4.0, 2.0), pr, "same")
        assert final == "same" and edge_id
        assert await conn.fetchval("SELECT confidence_alpha FROM atoms WHERE id = $1", old) == alpha0
        meta = json.loads(await conn.fetchval("SELECT metadata FROM edges WHERE id = $1", edge_id))
        assert meta["evidence_added"] is False
