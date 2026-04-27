"""create_edge persists optional metadata JSONB."""
import json

import pytest


@pytest.mark.asyncio
async def test_create_edge_persists_metadata(pool, agent_with_address):
    from mnemo.server.services.atom_service import create_edge, _insert_atom
    from mnemo.server.decomposer import DecomposedAtom
    from mnemo.server.embeddings import encode

    agent_id = agent_with_address["id"]

    async with pool.acquire() as conn:
        emb_a = await encode("alpha fact one")
        emb_b = await encode("alpha fact two")
        a = await _insert_atom(
            conn, agent_id,
            DecomposedAtom(text="alpha fact one", atom_type="semantic",
                           confidence_alpha=4.0, confidence_beta=2.0),
            emb_a, ["t"], "direct_experience",
        )
        b = await _insert_atom(
            conn, agent_id,
            DecomposedAtom(text="alpha fact two", atom_type="semantic",
                           confidence_alpha=4.0, confidence_beta=2.0),
            emb_b, ["t"], "direct_experience",
        )
        result = await create_edge(
            conn=conn, source_id=a["id"], target_id=b["id"],
            edge_type="tension_with", weight=0.85,
            metadata={"reasoning": "test", "detector": "auto_lifecycle_v1"},
        )
        assert result is not None
        row = await conn.fetchrow(
            "SELECT metadata FROM edges WHERE id = $1", result["id"],
        )
        assert json.loads(row["metadata"]) == {
            "reasoning": "test",
            "detector": "auto_lifecycle_v1",
        }
