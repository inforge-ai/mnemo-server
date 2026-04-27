"""Unit tests for lifecycle_service. LLM call is mocked; DB is real."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mnemo.server.decomposer import DecomposedAtom
from mnemo.server.embeddings import encode


async def _insert(conn, agent_id, text, atom_type="semantic"):
    from mnemo.server.services.atom_service import _insert_atom
    emb = await encode(text)
    row = await _insert_atom(
        conn, agent_id,
        DecomposedAtom(text=text, atom_type=atom_type,
                       confidence_alpha=4.0, confidence_beta=2.0),
        emb, ["t"], "direct_experience",
    )
    return row["id"], emb


# ── Candidate query ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_candidates_filters_to_band(pool, agent_with_address):
    from mnemo.server.services.lifecycle_service import _get_candidates

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        new_id, new_emb = await _insert(conn, agent_id, "Zulip integration is complete and in daily use")
        same_id, _ = await _insert(conn, agent_id, "Zulip integration is a planned future task")
        off_id, _ = await _insert(conn, agent_id, "Pluto is a dwarf planet in the Kuiper belt")

        candidates = await _get_candidates(conn, agent_id, new_id, new_emb)

    cand_ids = {c["id"] for c in candidates}
    assert same_id in cand_ids
    assert off_id not in cand_ids
    for c in candidates:
        assert "text_content" in c and "similarity" in c and "atom_type" in c
        assert 0.50 <= c["similarity"] < 0.90


@pytest.mark.asyncio
async def test_get_candidates_excludes_self_and_inactive(pool, agent_with_address):
    from mnemo.server.services.lifecycle_service import _get_candidates

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        new_id, new_emb = await _insert(conn, agent_id, "Zulip integration is complete and in daily use")
        other_id, _ = await _insert(conn, agent_id, "Zulip integration is a planned future task")
        await conn.execute("UPDATE atoms SET is_active = false WHERE id = $1", other_id)
        candidates = await _get_candidates(conn, agent_id, new_id, new_emb)

    assert all(c["id"] != new_id for c in candidates)
    assert all(c["id"] != other_id for c in candidates)
