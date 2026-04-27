"""End-to-end lifecycle eval — nine cases from docs/episodic_suppression-tension.md.

These tests:
- Hit the live Haiku decomposer + the live lifecycle LLM.
- Run only with `pytest -m eval`. Slow and consumes Anthropic API budget.

Each case stores 1+ atoms via /remember (which awaits store_background inline
under MNEMO_SYNC_STORE_FOR_TESTS=true) then issues a /recall and asserts on
edge state in the DB and lifecycle_edges metadata in the recall response.
"""

import os
from uuid import UUID

import pytest

from tests.conftest import remember as remember_helper

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="eval requires ANTHROPIC_API_KEY for the decomposer + lifecycle LLM",
    ),
]


async def _recall(client, agent_key: str, agent_id: str, query: str, max_results: int = 10):
    headers = {"X-Agent-Key": agent_key}
    resp = await client.post(
        f"/v1/agents/{agent_id}/recall",
        json={"query": query, "max_results": max_results},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _edges_of_type(pool, agent_id: UUID, edge_type: str) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.source_id, e.target_id, e.weight, e.edge_type
            FROM edges e
            JOIN atoms src ON src.id = e.source_id
            JOIN atoms tgt ON tgt.id = e.target_id
            WHERE e.edge_type = $2
              AND src.agent_id = $1
              AND tgt.agent_id = $1
            """,
            agent_id, edge_type,
        )
    return [dict(r) for r in rows]


def _lifecycle_edges_in_recall(result: dict, edge_type: str) -> list[dict]:
    found = []
    for atom in result.get("atoms", []):
        for edge in atom.get("lifecycle_edges") or []:
            if edge.get("relationship") == edge_type:
                found.append(edge)
    return found


# ── Case 1: State change (supersedes) ────────────────────────────────────────

async def test_case_1_state_change_supersedes(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Zulip integration is a planned future task", headers=headers)
    await remember_helper(client, aid, "Zulip integration is complete and in daily use", headers=headers)

    result = await _recall(client, agent_key, aid, "Zulip integration status")
    texts = " || ".join(a["text_content"] for a in result["atoms"]).lower()
    assert "complete" in texts
    assert "planned" not in texts, f"planned atom not superseded: {texts}"

    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert len(sup) >= 1


# ── Case 2: Preference change (supersedes) ───────────────────────────────────

async def test_case_2_preference_change_supersedes(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Tom prefers Mattermost for team communication", headers=headers)
    await remember_helper(client, aid, "Tom now prefers Zulip; Mattermost has been replaced", headers=headers)

    result = await _recall(client, agent_key, aid, "Tom communication preferences")
    texts = " || ".join(a["text_content"] for a in result["atoms"]).lower()
    assert "zulip" in texts
    standalone_old = [
        a for a in result["atoms"]
        if "mattermost" in a["text_content"].lower()
        and "zulip" not in a["text_content"].lower()
        and "replaced" not in a["text_content"].lower()
    ]
    assert standalone_old == [], f"old preference atom not superseded: {standalone_old}"

    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert len(sup) >= 1


# ── Case 3: Dedup-by-rephrasing (control: NO lifecycle edge) ────────────────

async def test_case_3_dedup_by_rephrasing_no_edge(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "test tasks consumed 89% of spend", headers=headers)
    await remember_helper(client, aid, "test tasks were cost black holes consuming 89%", headers=headers)

    # Phase 3 sentinel: recall must expose a lifecycle_edges key on every atom.
    # Pre-Phase-3 the field does not exist on AtomResponse, so this fails and
    # the strict xfail holds. Post-Phase-3 the field is at minimum [].
    result = await _recall(client, agent_key, aid, "test task spend")
    assert result["atoms"], "recall returned no atoms — dedup should have produced at least one"
    for atom in result["atoms"]:
        assert "lifecycle_edges" in atom, "recall atom missing lifecycle_edges (Phase 3 contract)"

    for et in ("supersedes", "tension_with", "narrows"):
        edges = await _edges_of_type(pool, agent_uuid, et)
        assert edges == [], f"unexpected {et} edge in dedup band: {edges}"


# ── Case 4: Episodic correction (supersedes) ─────────────────────────────────

async def test_case_4_episodic_correction_supersedes(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Mnemo achieves 76.1% on LoCoMo benchmark", headers=headers)
    await remember_helper(
        client, aid,
        "Actually Mnemo achieves 82.1% on LoCoMo; 76.1% was the gte-small result",
        headers=headers,
    )

    result = await _recall(client, agent_key, aid, "Mnemo LoCoMo score")
    texts = " || ".join(a["text_content"] for a in result["atoms"])
    assert "82.1" in texts
    standalone_old = [
        a for a in result["atoms"]
        if "76.1" in a["text_content"] and "82.1" not in a["text_content"]
    ]
    assert standalone_old == [], f"old atom not superseded: {standalone_old}"

    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert len(sup) >= 1


# ── Case 5: Stale-but-not-superseded (control: NO edge, classified independent) ─

async def test_case_5_independent_no_edge(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Tom is co-founder of Inforge LLC", headers=headers)
    await remember_helper(client, aid, "Inforge LLC was incorporated in Delaware in March 2023", headers=headers)

    # Phase 3 sentinel: recall must expose a lifecycle_edges key on every atom.
    result = await _recall(client, agent_key, aid, "Inforge company status")
    assert result["atoms"]
    for atom in result["atoms"]:
        assert "lifecycle_edges" in atom, "recall atom missing lifecycle_edges (Phase 3 contract)"

    for et in ("supersedes", "tension_with", "narrows"):
        edges = await _edges_of_type(pool, agent_uuid, et)
        assert edges == [], f"facet additions should not create {et}: {edges}"


# ── Case 6: Narrowing (narrows edge + lifecycle metadata in recall) ──────────

async def test_case_6_narrowing_creates_narrows_edge(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Tom uses Mattermost for all communication", headers=headers)
    await remember_helper(
        client, aid,
        "Tom uses Zulip for Inforge ops; Mattermost for personal",
        headers=headers,
    )

    result = await _recall(client, agent_key, aid, "Tom communication tools")
    texts = " || ".join(a["text_content"] for a in result["atoms"]).lower()
    assert "mattermost" in texts
    assert "zulip" in texts

    narrows = await _edges_of_type(pool, agent_uuid, "narrows")
    assert len(narrows) >= 1
    surfaced = _lifecycle_edges_in_recall(result, "narrows")
    assert len(surfaced) >= 1, f"recall response missing lifecycle_edges narrows: {result}"


# ── Case 7: Semantic tension (tension_with, NOT supersedes) ──────────────────

async def test_case_7_semantic_tension_not_supersedes(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(
        client, aid,
        "Newtonian gravity accurately predicts planetary orbits",
        headers=headers,
    )
    await remember_helper(
        client, aid,
        "Mercury's perihelion precesses by 43 arcseconds per century beyond Newtonian prediction",
        headers=headers,
    )

    result = await _recall(client, agent_key, aid, "Newtonian gravity validity")
    texts = " || ".join(a["text_content"] for a in result["atoms"]).lower()
    assert "newtonian" in texts
    assert "mercury" in texts or "perihelion" in texts

    tension = await _edges_of_type(pool, agent_uuid, "tension_with")
    assert len(tension) >= 1
    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert sup == [], f"semantic claim incorrectly superseded: {sup}"
    surfaced = _lifecycle_edges_in_recall(result, "tension_with")
    assert len(surfaced) >= 1


# ── Case 8: Benchmark tension (tension_with) ─────────────────────────────────

async def test_case_8_benchmark_tension(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(
        client, aid,
        "Mnemo achieves 82.1% on LoCoMo multi-hop, best-in-class",
        headers=headers,
    )
    await remember_helper(
        client, aid,
        "Hindsight achieves 91.4% on LongMemEval, exceeding Mnemo",
        headers=headers,
    )

    result = await _recall(client, agent_key, aid, "Mnemo competitive position on memory benchmarks")
    texts = " || ".join(a["text_content"] for a in result["atoms"]).lower()
    assert "mnemo" in texts
    assert "hindsight" in texts

    tension = await _edges_of_type(pool, agent_uuid, "tension_with")
    assert len(tension) >= 1
    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert sup == [], f"competitive benchmarks should not supersede: {sup}"


# ── Case 9: Episodic measurement correction (supersedes, NOT tension) ────────

async def test_case_9_episodic_measurement_supersedes(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Q3 revenue forecast is $4.2M", headers=headers)
    await remember_helper(
        client, aid,
        "Corrected Q3 revenue forecast is $3.8M; the $4.2M number had a calculation error",
        headers=headers,
    )

    result = await _recall(client, agent_key, aid, "Q3 revenue forecast")
    texts = " || ".join(a["text_content"] for a in result["atoms"])
    assert "$3.8M" in texts or "3.8M" in texts
    standalone_old = [
        a for a in result["atoms"]
        if "$4.2M" in a["text_content"] and "$3.8M" not in a["text_content"] and "3.8M" not in a["text_content"]
    ]
    assert standalone_old == [], f"old measurement not superseded: {standalone_old}"

    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert len(sup) >= 1
    tension = await _edges_of_type(pool, agent_uuid, "tension_with")
    assert tension == [], f"correction should not be a tension: {tension}"
