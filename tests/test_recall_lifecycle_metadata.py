"""Recall response carries lifecycle_edges metadata for tension_with / narrows
edges. Supersedes edges remain hidden by _filter_superseded."""
import pytest

from mnemo.server.services.atom_service import _insert_atom, create_edge, retrieve
from mnemo.server.decomposer import DecomposedAtom
from mnemo.server.embeddings import encode


async def _insert(conn, agent_id, text, atom_type="semantic"):
    emb = await encode(text)
    row = await _insert_atom(
        conn, agent_id,
        DecomposedAtom(text=text, atom_type=atom_type,
                       confidence_alpha=4.0, confidence_beta=2.0),
        emb, ["t"], "direct_experience",
    )
    return row["id"]


@pytest.mark.asyncio
async def test_recall_attaches_tension_with_edges(pool, agent_with_address):
    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        a_id = await _insert(conn, agent_id, "Newtonian gravity accurately predicts orbits")
        b_id = await _insert(conn, agent_id, "Mercury's perihelion precesses anomalously")
        await create_edge(
            conn=conn, source_id=b_id, target_id=a_id,
            edge_type="tension_with", weight=0.78,
            metadata={"reasoning": "anomaly", "detector": "auto_lifecycle_v1"},
        )

        result = await retrieve(
            conn=conn, agent_id=agent_id, query="Newtonian gravity validity",
            domain_tags=None, min_confidence=0.0, min_similarity=0.0,
            max_results=10, expand_graph=False, expansion_depth=0,
            include_superseded=False, similarity_drop_threshold=None,
            verbosity="standard", max_content_chars=None, max_total_tokens=None,
        )

    by_id = {a["id"]: a for a in result["atoms"]}
    assert a_id in by_id and b_id in by_id

    a_edges = by_id[a_id].get("lifecycle_edges") or []
    b_edges = by_id[b_id].get("lifecycle_edges") or []
    # Both endpoints expose the tension; the relationship is symmetric in surface.
    assert any(
        e["related_atom_id"] == b_id and e["relationship"] == "tension_with"
        for e in a_edges
    ), f"a_edges: {a_edges}"
    assert any(
        e["related_atom_id"] == a_id and e["relationship"] == "tension_with"
        for e in b_edges
    ), f"b_edges: {b_edges}"


@pytest.mark.asyncio
async def test_recall_does_not_surface_supersedes_in_lifecycle_edges(pool, agent_with_address):
    """supersedes is filtered server-side; the surviving atom doesn't carry
    a lifecycle_edges entry pointing at the retired atom."""
    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        old_id = await _insert(conn, agent_id, "Zulip integration is a planned future task", "episodic")
        new_id = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic")
        await create_edge(
            conn=conn, source_id=new_id, target_id=old_id,
            edge_type="supersedes", weight=0.9,
            metadata={"reasoning": "x", "detector": "auto_lifecycle_v1"},
        )

        result = await retrieve(
            conn=conn, agent_id=agent_id, query="Zulip integration status",
            domain_tags=None, min_confidence=0.0, min_similarity=0.0,
            max_results=10, expand_graph=False, expansion_depth=0,
            include_superseded=False, similarity_drop_threshold=None,
            verbosity="standard", max_content_chars=None, max_total_tokens=None,
        )

    ids = [a["id"] for a in result["atoms"]]
    assert old_id not in ids
    new_atom = next(a for a in result["atoms"] if a["id"] == new_id)
    assert (new_atom.get("lifecycle_edges") or []) == []
