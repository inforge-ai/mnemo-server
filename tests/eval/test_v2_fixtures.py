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

Cases the current system fails carry "xfail": {"phase": ..., "fix": ...} and
run as xfail(strict=True), so a fix that makes one pass must also flip it.
"strict": false marks a case that fails only some of the time (LLM
nondeterminism); flip it to strict once the fix lands.

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


def _load_cases():
    params = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        case = json.loads(path.read_text())
        marks = []
        if case.get("xfail"):
            x = case["xfail"]
            marks.append(pytest.mark.xfail(
                strict=x.get("strict", True),
                reason=f"{x.get('phase', 'phase ?')}: {x.get('fix', '')}",
            ))
        params.append(pytest.param(case, id=case["id"], marks=marks))
    return params


def _matches(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


async def _wait_for_store(client, store_id: str, headers: dict, timeout: float = 60.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/stores/{store_id}/status", headers=headers)
        if r.status_code == 200 and r.json()["status"] in ("complete", "failed"):
            return
        await asyncio.sleep(0.25)


@pytest.mark.parametrize("case", _load_cases())
async def test_v2_fixture(case, client, agent_with_key, pool, monkeypatch):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])

    sync = case.get("sync_store", True)
    monkeypatch.setattr(settings, "sync_store_for_tests", sync)
    # Match the Phase 0 production setting (MNEMO_DUPLICATE_SIMILARITY_THRESHOLD).
    monkeypatch.setattr(settings, "duplicate_similarity_threshold", 0.97)

    store_ids = []
    for step in case["steps"]:
        body = {"text": step["text"]}
        if step.get("remembered_on"):
            body["remembered_on"] = step["remembered_on"]
        r = await client.post(f"/v1/agents/{aid}/remember", json=body, headers=headers)
        assert r.status_code == 201, r.text
        store_ids.append(r.json()["store_id"])

    try:
        await _check(case, client, pool, aid, agent_key)
    finally:
        if not sync:  # let background stores finish before the DB is cleaned
            for sid in store_ids:
                await _wait_for_store(client, sid, headers)


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
