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


# ── LLM caller ──────────────────────────────────────────────────────────────

def _mock_haiku(payload_json: str, input_tokens: int = 100, output_tokens: int = 30):
    msg = MagicMock()
    msg.content = [MagicMock(text=payload_json)]
    msg.model = "claude-haiku-4-5-20251001"
    msg.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return msg


@pytest.mark.asyncio
async def test_evaluate_pair_parses_supersedes():
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    fake = _mock_haiku(
        '{"relationship": "supersedes", "confidence": 0.92, '
        '"reasoning": "new atom marks the planned task as complete"}'
    )
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake)

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="Zulip integration is complete and in daily use",
            new_type="episodic",
            existing_text="Zulip integration is a planned future task",
            existing_type="episodic",
            existing_age_days=30,
        )

    assert result["relationship"] == "supersedes"
    assert result["confidence"] == 0.92
    assert "complete" in result["reasoning"]
    assert result["usage"]["input_tokens"] == 100


@pytest.mark.asyncio
async def test_evaluate_pair_parses_tension_with():
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    fake = _mock_haiku(
        '{"relationship": "tension_with", "confidence": 0.78, '
        '"reasoning": "anomaly does not invalidate Newtonian framework"}'
    )
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake)

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="Mercury's perihelion precesses anomalously",
            new_type="semantic",
            existing_text="Newtonian gravity accurately predicts orbits",
            existing_type="semantic",
            existing_age_days=2,
        )

    assert result["relationship"] == "tension_with"
    assert result["confidence"] == 0.78


@pytest.mark.asyncio
async def test_evaluate_pair_strips_markdown_fences():
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    fake = _mock_haiku(
        '```json\n{"relationship": "narrows", "confidence": 0.70, '
        '"reasoning": "qualifies the original"}\n```'
    )
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake)

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="Tom uses Zulip for ops, Mattermost for personal",
            new_type="semantic",
            existing_text="Tom uses Mattermost",
            existing_type="semantic",
            existing_age_days=1,
        )

    assert result["relationship"] == "narrows"
    assert result["confidence"] == 0.70


@pytest.mark.asyncio
async def test_evaluate_pair_returns_none_on_unknown_relationship():
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    fake = _mock_haiku('{"relationship": "weird_made_up", "confidence": 0.9}')
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake)

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="x", new_type="semantic",
            existing_text="y", existing_type="semantic",
            existing_age_days=1,
        )
    assert result is None


@pytest.mark.asyncio
async def test_evaluate_pair_retries_once_then_returns_none():
    """Spec §4: single retry on transient error, then give up."""
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=RuntimeError("transient"))

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="x", new_type="semantic",
            existing_text="y", existing_type="semantic",
            existing_age_days=1,
        )

    assert result is None
    assert mock_client.messages.create.await_count == 2  # initial + 1 retry


@pytest.mark.asyncio
async def test_evaluate_pair_first_attempt_fails_second_succeeds():
    """Retry recovers when the first call raises and the second returns valid JSON."""
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    fake_success = _mock_haiku(
        '{"relationship": "supersedes", "confidence": 0.9, "reasoning": "ok"}'
    )
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=[
        RuntimeError("transient"),
        fake_success,
    ])

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="x", new_type="episodic",
            existing_text="y", existing_type="episodic",
            existing_age_days=1,
        )

    assert result is not None
    assert result["relationship"] == "supersedes"
    assert mock_client.messages.create.await_count == 2


@pytest.mark.asyncio
async def test_evaluate_pair_returns_none_on_invalid_json_no_retry():
    """Garbled JSON is permanent — return None, do not retry."""
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    fake = _mock_haiku("this is not json at all")
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake)

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="x", new_type="semantic",
            existing_text="y", existing_type="semantic",
            existing_age_days=1,
        )

    assert result is None
    assert mock_client.messages.create.await_count == 1


@pytest.mark.asyncio
async def test_evaluate_pair_returns_none_on_non_numeric_confidence_no_retry():
    """Bad confidence value is permanent — return None, do not retry."""
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    fake = _mock_haiku('{"relationship": "supersedes", "confidence": "high"}')
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake)

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="x", new_type="semantic",
            existing_text="y", existing_type="semantic",
            existing_age_days=1,
        )

    assert result is None
    assert mock_client.messages.create.await_count == 1
