"""v2 Phase 0 item 7: small safe fixes."""

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from mnemo.server.decomposer import DecomposedAtom
from mnemo.server.embeddings import encode
from mnemo.server.llm_decomposer import DecomposerResult
from mnemo.server.services import atom_service
from tests.test_consolidation import _insert_atom_sql


# ── store_explicit dedups within the requested atom_type only ────────────────

async def test_store_explicit_does_not_merge_across_types(client, agent, pool):
    agent_id = UUID(agent["id"])
    headers = {"X-Agent-Key": agent["agent_key"]}
    text = "Run the database migrations before restarting the server."
    async with pool.acquire() as conn:
        semantic_id = await _insert_atom_sql(
            conn, agent_id, "semantic", text, await encode(text), decay_type="none",
        )

    resp = await client.post(
        f"/v1/agents/{agent_id}/atoms",
        json={"atom_type": "procedural", "text_content": text, "domain_tags": ["ops"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] != str(semantic_id)
    assert body["atom_type"] == "procedural"


# ── decomposer_version reflects what actually ran ────────────────────────────

def _result(usage):
    return DecomposerResult(
        atoms=[DecomposedAtom(
            text="The nightly backup job writes to the cold storage bucket.",
            atom_type="semantic", confidence_alpha=4.0, confidence_beta=2.0,
        )],
        usage=usage,
    )


@pytest.mark.parametrize("usage, expected", [
    (None, "regex_v1"),
    ({"model": "m", "input_tokens": 1, "output_tokens": 1}, "haiku_v1"),
])
async def test_decomposer_version_from_result(agent, pool, monkeypatch, usage, expected):
    # API key present in both cases: the version must not depend on it.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    agent_id = UUID(agent["id"])
    with patch.object(atom_service, "_decompose", AsyncMock(return_value=_result(usage))):
        async with pool.acquire() as conn:
            out = await atom_service.store_from_text(conn, agent_id, "irrelevant", ["ops"])
            assert out["atoms_created"] == 1
            version = await conn.fetchval(
                "SELECT decomposer_version FROM atoms WHERE id = $1", out["new_atom_ids"][0],
            )
    assert version == expected


# ── RetrieveRequest bounds ───────────────────────────────────────────────────

@pytest.mark.parametrize("field, value", [
    ("max_results", 0), ("max_results", 51),
    ("expansion_depth", -1), ("expansion_depth", 3),
    ("min_confidence", -0.1), ("min_confidence", 1.1),
])
async def test_recall_rejects_out_of_range(client, agent, field, value):
    resp = await client.post(
        f"/v1/agents/{agent['id']}/recall",
        json={"query": "anything", field: value},
        headers={"X-Agent-Key": agent["agent_key"]},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("field, value", [
    ("max_results", 1), ("max_results", 50),
    ("expansion_depth", 0), ("expansion_depth", 2),
    ("min_confidence", 0.0), ("min_confidence", 1.0),
])
async def test_recall_accepts_bounds(client, agent, field, value):
    resp = await client.post(
        f"/v1/agents/{agent['id']}/recall",
        json={"query": "anything", field: value},
        headers={"X-Agent-Key": agent["agent_key"]},
    )
    assert resp.status_code == 200, resp.text


# ── _create_similarity_edges skips same-id pairs ─────────────────────────────

async def test_similarity_edges_skip_same_id(agent, pool):
    agent_id = UUID(agent["id"])
    t1 = "The ingest worker retries failed batches three times."
    t2 = "The ingest worker retries a failed batch up to three times."
    e1, e2 = await encode(t1), await encode(t2)
    async with pool.acquire() as conn:
        a = await _insert_atom_sql(conn, agent_id, "semantic", t1, e1, decay_type="none")
        b = await _insert_atom_sql(conn, agent_id, "semantic", t2, e2, decay_type="none")
        # Same id twice (two decomposed atoms deduped into one) plus a real pair.
        created = await atom_service._create_similarity_edges(
            conn, [a, a, b], [e1, e1, e2], threshold=0.5,
        )
        loops = await conn.fetchval(
            "SELECT count(*) FROM edges WHERE source_id = target_id AND source_id = ANY($1)", [a, b],
        )
        pair = await conn.fetchval(
            "SELECT count(*) FROM edges WHERE source_id = $1 AND target_id = $2", a, b,
        )
    assert loops == 0
    assert pair == 1
    assert created >= 1
