"""v2 regression fixtures (Phase 0 item 6).

One synthetic case per JSON file in tests/fixtures/v2/. Each case stores its
steps via /remember (optionally with remembered_on), then asserts on the
agent's active atoms and/or a /recall. Regex assertions are case-insensitive
and run against each atom's text separately:

  atoms_any_match   [re]  every pattern matches at least one active atom
  atoms_none_match  [re]  no pattern matches any active atom
  recall_top_match  re    the first recall result matches
  recall_any_match  [re]  every pattern matches at least one recall result
  recall_none_match [re]  no pattern matches any recall result
  remembered_on_any_between [lo, hi]  at least one active atom has
                                      lo <= remembered_on <= hi (ISO dates)
  remembered_on_none_before  date     no active atom has remembered_on < date

v2 Phase 1 additions:
  - every live case runs MNEMO_FIXTURE_REPEAT times (default 5) and must pass
    each time; a case can set "repeat" to override (e.g. deterministic ones)
  - steps may set speaker / addressee / channel / origin_class
  - a step may set "filler": {"paragraph": "... {i} ...", "count": N} to
    prepend N generated synthetic paragraphs (the long-note case)
  - "simulate": "provider_failure" swaps in the fake decomposer raising a
    DecomposerError; assert keys "http_status", "source_kept", "atom_count"

v2 Phase 2 additions (relations decided on write):
  - every case runs with MNEMO_FIXTURE_RELATION_MODE (default "enforce") unless
    it sets "relation_mode"; decisions use the live OpenRouter models
    (OPENROUTER_API_KEY, every request with provider {"zdr": true}); the
    relation budget is raised so fixtures test decisions, not latency
  - "relations": [{"new": i, "old": j, "any": rel} | {"new": i, "old": j, "none": rel}]
    checks relation_decisions between the atoms of step i and step j
    (`any`: at least one pair has final relation rel; `none`: no pair has it)
  - "recall_contains_steps": [i]  recall returns at least one atom of step i
  - "recall_hides_steps": [i]     recall returns no atom of step i
  - "conflict_open": true         an open conflict joins an atom of step 0 and of step 1
  - "undo_replaces": true         undo every replaces edge, then recall_contains_steps
                                  must hold for the restored step(s)
  - a step with "explicit": "<atom_type>" is stored through POST /atoms (no
    decomposition, no date, no checks), like an atom stored before Phase 2
  - "simulate": "decision_outage" the decision model is down: every step is
    stored (201), relations are pending, nothing is hidden
  - every case runs with the kind matrix MNEMO_FIXTURE_RELATION_MATRIX
    (default "as_built"; "state_rule_symmetric" is the G3 candidate);
    "xfail_at_matrix": {matrix: xfail} overrides "xfail" for that matrix

Cases the current system fails carry "xfail": {"phase": ..., "fix": ...} and
run as xfail(strict=True), so a fix that makes one pass must also flip it.
"strict": false marks a case that fails only some of the time (LLM
nondeterminism). Cases run at the Phase 0 production dedup threshold unless
they list `dedup_thresholds`; `xfail_at_threshold` sets per-threshold xfails.

Hits the live decomposer (and lifecycle LLM): run with `pytest -m eval`.
Fixture text is synthetic. Never copy stored atoms or raw feedback here.
"""

import asyncio
import json
import os
import re
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

from mnemo.server.config import settings

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "v2"

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="eval requires ANTHROPIC_API_KEY for the decomposer",
    ),
]


PHASE0_DEDUP_THRESHOLD = 0.97  # MNEMO_DUPLICATE_SIMILARITY_THRESHOLD in production
DEFAULT_REPEAT = int(os.environ.get("MNEMO_FIXTURE_REPEAT", "5"))
STEP_FIELDS = ("remembered_on", "speaker", "addressee", "channel", "origin_class")
RELATION_MODE = os.environ.get("MNEMO_FIXTURE_RELATION_MODE", "enforce")
RELATION_MATRIX = os.environ.get("MNEMO_FIXTURE_RELATION_MATRIX", "as_built")


def _xfail_mark(x: dict):
    return pytest.mark.xfail(
        strict=x.get("strict", True),
        reason=f"{x.get('phase', 'phase ?')}: {x.get('fix', '')}",
    )


def _load_cases():
    """One param per (case, dedup threshold). `dedup_thresholds` defaults to
    the Phase 0 production setting; `xfail_at_threshold` overrides `xfail`
    for a given threshold (keys are str(float), e.g. "0.9")."""
    params = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        case = json.loads(path.read_text())
        thresholds = case.get("dedup_thresholds", [PHASE0_DEDUP_THRESHOLD])
        repeat = case.get("repeat", DEFAULT_REPEAT)
        for t in thresholds:
            x = case.get("xfail_at_threshold", {}).get(str(float(t)), case.get("xfail"))
            if RELATION_MATRIX in case.get("xfail_at_matrix", {}):
                x = case["xfail_at_matrix"][RELATION_MATRIX]
            marks = [_xfail_mark(x)] if x else []
            pid = case["id"] if len(thresholds) == 1 else f"{case['id']}@{t}"
            for k in range(1, repeat + 1):
                rid = pid if repeat == 1 else f"{pid}#{k}"
                params.append(pytest.param(case, t, id=rid, marks=marks))
    return params


def _step_text(step: dict) -> str:
    filler = step.get("filler")
    if not filler:
        return step["text"]
    paras = [filler["paragraph"].format(i=i) for i in range(1, filler["count"] + 1)]
    return "\n\n".join(paras + [step["text"]])


def _matches(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


async def _wait_for_store(client, store_id: str, headers: dict, timeout: float = 60.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/stores/{store_id}/status", headers=headers)
        if r.status_code == 200 and r.json()["status"] in ("complete", "failed"):
            return
        await asyncio.sleep(0.25)


@pytest.mark.parametrize("case, dedup_threshold", _load_cases())
async def test_v2_fixture(case, dedup_threshold, client, agent_with_key, pool, monkeypatch):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])

    sync = case.get("sync_store", True)
    monkeypatch.setattr(settings, "sync_store_for_tests", sync)
    monkeypatch.setattr(settings, "duplicate_similarity_threshold", dedup_threshold)

    if case.get("simulate") == "provider_failure":
        await _provider_failure(case, client, pool, aid, headers, monkeypatch)
        return

    monkeypatch.setattr(settings, "relation_mode", case.get("relation_mode", RELATION_MODE))
    monkeypatch.setattr(settings, "relation_budget_seconds", 30.0)
    monkeypatch.setattr(settings, "relation_matrix", RELATION_MATRIX)
    if case.get("simulate") == "decision_outage":
        from mnemo.server.decisions.fake import FakeDecisions
        from mnemo.server.services import relation_service

        def down(state, questions):
            raise RuntimeError("decision provider down")
        monkeypatch.setattr(relation_service, "get_decision_model", lambda: FakeDecisions(down))

    store_ids = []
    for step in case["steps"]:
        if step.get("explicit"):
            r = await client.post(f"/v1/agents/{aid}/atoms", headers=headers,
                                  json={"atom_type": step["explicit"], "text_content": step["text"]})
            assert r.status_code == 201, r.text
            store_ids.append(("atom", r.json()["id"]))
            continue
        body = {"text": _step_text(step)}
        for f in STEP_FIELDS:
            if step.get(f):
                body[f] = step[f]
        r = await client.post(f"/v1/agents/{aid}/remember", json=body, headers=headers)
        assert r.status_code == 201, r.text
        store_ids.append(r.json()["store_id"])
        if case.get("simulate") == "decision_outage" and settings.relation_mode != "off" and r.json().get("atoms"):
            assert r.json().get("relations_pending") or step is case["steps"][0], r.text

    try:
        await _check(case, client, pool, aid, agent_key, store_ids)
    finally:
        if not sync:  # let background stores finish before the DB is cleaned
            for sid in store_ids:
                if not isinstance(sid, tuple):
                    await _wait_for_store(client, sid, headers)


async def _provider_failure(case, client, pool, aid, headers, monkeypatch):
    """The decomposer fails: 502, the source row exists, no atoms stored."""
    from mnemo.server.decomposers import DecomposerError, fake

    monkeypatch.setattr(settings, "decomposer_provider", "fake")
    a = case["assert"]
    for step in case["steps"]:
        fake.script(DecomposerError("provider timeout"))
        r = await client.post(f"/v1/agents/{aid}/remember", json={"text": step["text"]}, headers=headers)
        assert r.status_code == a["http_status"], r.text
        body = r.json()
        assert body["status"] == "failed" and body["error"] == "provider timeout"
        async with pool.acquire() as conn:
            kept = await conn.fetchval("SELECT text FROM sources WHERE id = $1", UUID(body["source_id"]))
            n = await conn.fetchval("SELECT count(*) FROM atoms WHERE agent_id = $1", UUID(aid))
        assert (kept == step["text"]) is a["source_kept"]
        assert n == a["atom_count"]


async def _step_atoms(pool, store_ids) -> list[set]:
    out = []
    async with pool.acquire() as conn:
        for sid in store_ids:
            if isinstance(sid, tuple):           # explicit atom
                out.append({UUID(sid[1])})
                continue
            out.append({r["id"] for r in await conn.fetch(
                "SELECT a.id FROM atoms a JOIN sources s ON s.id = a.source_id WHERE s.store_id = $1", UUID(sid))})
    return out


async def _check_relations(case, pool, aid, agent_key, client, steps, failures):
    a = case["assert"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT new_atom_id, old_atom_id, final_relation, guard_fired, raw_choice, inputs, probabilities "
            "FROM relation_decisions WHERE agent_id = $1", UUID(aid))

    def _why(r):   # synthetic fixtures only: safe to print
        i = json.loads(r["inputs"] or "{}")
        p = json.loads(r["probabilities"] or "{}")
        rel = {k: round(v, 2) for k, v in (p.get("relation") or {}).items()}
        return (f"{i.get('new', {}).get('kind')}({i.get('new_kind_p')})->{i.get('old', {}).get('kind')}"
                f"[top {i.get('old_kind_top')}]"
                f"({i.get('old_kind_p')}) raw={r['raw_choice']} rel={rel} chg={round(p.get('change', 0), 2)} "
                f"attr={round(p.get('same_attribute', 0), 2)} corr={round(p.get('correction', 0), 2)} "
                f"prec={i.get('new', {}).get('precision')}/{i.get('old', {}).get('precision')}")
    for spec in a.get("relations", []):
        new, old = steps[spec["new"]], steps[spec["old"]]
        finals = [r["final_relation"] for r in rows if r["new_atom_id"] in new and r["old_atom_id"] in old]
        guards = [r["guard_fired"] for r in rows if r["new_atom_id"] in new and r["old_atom_id"] in old]
        guards += [_why(r) for r in rows if r["new_atom_id"] in new and r["old_atom_id"] in old]
        if "any" in spec and spec["any"] not in finals:
            failures.append(f"step {spec['new']} -> step {spec['old']}: no {spec['any']} (got {finals}, guards {guards})")
        if "none" in spec and spec["none"] in finals:
            failures.append(f"step {spec['new']} -> step {spec['old']}: unexpected {spec['none']} (guards {guards})")
    if a.get("conflict_open"):
        async with pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM conflicts WHERE resolved_at IS NULL AND "
                "((atom_a = ANY($1) AND atom_b = ANY($2)) OR (atom_a = ANY($2) AND atom_b = ANY($1)))",
                list(steps[0]), list(steps[1]))
        if not n:
            failures.append("no open conflict between step 0 and step 1")
    if a.get("undo_replaces"):
        async with pool.acquire() as conn:
            edges = await conn.fetch("SELECT e.id FROM edges e JOIN atoms s ON s.id = e.source_id "
                                     "WHERE s.agent_id = $1 AND e.edge_type = 'replaces' AND e.retired_at IS NULL",
                                     UUID(aid))
        if not edges:
            failures.append("undo_replaces: no replaces edge to undo")
        for e in edges:
            r = await client.post(f"/v1/edges/{e['id']}/undo", json={}, headers={"X-Agent-Key": agent_key})
            if r.status_code != 200:
                failures.append(f"undo returned {r.status_code}")


async def _check(case, client, pool, aid, agent_key, store_ids=()):
    a = case["assert"]
    failures = []
    steps = await _step_atoms(pool, store_ids) if store_ids else []
    if steps:
        await _check_relations(case, pool, aid, agent_key, client, steps, failures)

    if "remembered_on_any_between" in a or "remembered_on_none_before" in a:
        async with pool.acquire() as conn:
            dates = [r["d"] for r in await conn.fetch(
                "SELECT remembered_on::date AS d FROM atoms "
                "WHERE agent_id = $1 AND is_active = true AND remembered_on IS NOT NULL",
                UUID(aid),
            )]
        if "remembered_on_any_between" in a:
            lo, hi = (date.fromisoformat(x) for x in a["remembered_on_any_between"])
            if not any(lo <= d <= hi for d in dates):
                failures.append(f"no atom has remembered_on in [{lo}, {hi}]; dates={sorted(map(str, dates))}")
        if "remembered_on_none_before" in a:
            floor = date.fromisoformat(a["remembered_on_none_before"])
            early = sorted(str(d) for d in dates if d < floor)
            if early:
                failures.append(f"{len(early)} atom(s) have remembered_on before {floor}: {early}")

    if "atoms_any_match" in a or "atoms_none_match" in a:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT text_content FROM atoms WHERE agent_id = $1 AND is_active = true",
                UUID(aid),
            )
        atoms = [r["text_content"] for r in rows]
        for p in a.get("atoms_any_match", []):
            if not any(_matches(p, t) for t in atoms):
                failures.append(f"no atom matches {p!r}")
        for p in a.get("atoms_none_match", []):
            hit = [t for t in atoms if _matches(p, t)]
            if hit:
                failures.append(f"{len(hit)} atom(s) match forbidden {p!r}")
        atoms_seen = atoms
    else:
        atoms_seen = None

    recalled = None
    if "recall" in case:
        rq = {"query": case["recall"]["query"], "max_results": case["recall"].get("max_results", 10)}
        r = await client.post(f"/v1/agents/{aid}/recall", json=rq, headers={"X-Agent-Key": agent_key})
        assert r.status_code == 200, r.text
        recalled = [x["text_content"] for x in r.json()["atoms"]]
        recalled_ids = {UUID(x["id"]) for x in r.json()["atoms"]}
        for i in a.get("recall_contains_steps", []):
            if not recalled_ids & steps[i]:
                failures.append(f"recall returns no atom of step {i}")
        for i in a.get("recall_hides_steps", []):
            if recalled_ids & steps[i]:
                failures.append(f"recall returns an atom of step {i}, which should be hidden")
        if "recall_top_match" in a:
            if not recalled or not _matches(a["recall_top_match"], recalled[0]):
                failures.append(f"top recall result does not match {a['recall_top_match']!r}")
        for p in a.get("recall_any_match", []):
            if not any(_matches(p, t) for t in recalled):
                failures.append(f"no recall result matches {p!r}")
        for p in a.get("recall_none_match", []):
            hit = [t for t in recalled if _matches(p, t)]
            if hit:
                failures.append(f"{len(hit)} recall result(s) match forbidden {p!r}")

    # Synthetic text only, so printing it on failure is safe.
    assert not failures, (
        f"{case['id']}: " + "; ".join(failures)
        + f"\n  atoms={atoms_seen}\n  recall={recalled}"
    )
