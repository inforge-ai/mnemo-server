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

v2 Phase 1 additions:
  - every live case runs MNEMO_FIXTURE_REPEAT times (default 5) and must pass
    each time; a case can set "repeat" to override (e.g. deterministic ones)
  - steps may set speaker / addressee / channel / origin_class
  - a step may set "filler": {"paragraph": "... {i} ...", "count": N} to
    prepend N generated synthetic paragraphs (the long-note case)
  - "simulate": "provider_failure" swaps in the fake decomposer raising a
    DecomposerError; assert keys "http_status", "source_kept", "atom_count"

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

    store_ids = []
    for step in case["steps"]:
        body = {"text": _step_text(step)}
        for f in STEP_FIELDS:
            if step.get(f):
                body[f] = step[f]
        r = await client.post(f"/v1/agents/{aid}/remember", json=body, headers=headers)
        assert r.status_code == 201, r.text
        store_ids.append(r.json()["store_id"])

    try:
        await _check(case, client, pool, aid, agent_key)
    finally:
        if not sync:  # let background stores finish before the DB is cleaned
            for sid in store_ids:
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


async def _check(case, client, pool, aid, agent_key):
    a = case["assert"]
    failures = []

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
