"""Unit tests for lifecycle_service. LLM call is mocked; DB is real."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from mnemo.server.decomposer import DecomposedAtom
from mnemo.server.embeddings import encode
from tests.helpers import vector_at_cosine


async def _insert(conn, agent_id, text, atom_type="semantic", emb=None):
    from mnemo.server.services.atom_service import _insert_atom
    if emb is None:
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
        same_id, _ = await _insert(conn, agent_id, "Zulip integration is a planned future task",
                                   emb=vector_at_cosine(new_emb, 0.70, seed=1))
        off_id, _ = await _insert(conn, agent_id, "Pluto is a dwarf planet in the Kuiper belt",
                                  emb=vector_at_cosine(new_emb, 0.10, seed=2))

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


def _eval_returning(payload):
    """side_effect helper for the AsyncMock-patched _evaluate_pair.

    patch.object on an async def auto-creates an AsyncMock. AsyncMock awaits
    an async side_effect through the mock; returning the payload directly
    from an async callable yields the payload as the awaited result.
    A sync callable returning a coroutine would NOT be awaited a second time
    and the orchestrator would receive the unfinished coroutine instead.
    """
    async def _impl(**kw):
        return payload
    return _impl


# ── Edge writes ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_writes_supersedes_at_or_above_threshold(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        old_id, old_emb = await _insert(conn, agent_id, "Zulip integration is a planned future task", "episodic")
        new_id, _ = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic",
                                  emb=vector_at_cosine(old_emb, 0.70, seed=3))

        payload = {
            "relationship": "supersedes",
            "confidence": 0.85,
            "reasoning": "marks planned task as complete",
            "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1},
        }
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_returning(payload)):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)

        assert n == 1
        edge = await conn.fetchrow(
            "SELECT edge_type, weight, metadata, decided_by FROM edges WHERE source_id=$1 AND target_id=$2",
            new_id, old_id,
        )
        assert edge["edge_type"] == "supersedes"
        assert edge["decided_by"] == "legacy_lifecycle"
        assert edge["weight"] == pytest.approx(0.85, abs=1e-6)
        meta = edge["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["detector"] == "auto_lifecycle_v1"
        assert meta["reasoning"].startswith("marks")
        assert "cosine_at_detection" in meta


@pytest.mark.asyncio
async def test_writes_tension_with_at_or_above_threshold(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        old_id, old_emb = await _insert(conn, agent_id, "Newtonian gravity accurately predicts orbits", "semantic")
        new_id, _ = await _insert(conn, agent_id, "Mercury's perihelion precesses anomalously", "semantic",
                                  emb=vector_at_cosine(old_emb, 0.70, seed=5))

        payload = {
            "relationship": "tension_with",
            "confidence": 0.70,
            "reasoning": "anomaly without invalidation",
            "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1},
        }
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_returning(payload)):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)

        assert n == 1
        edge = await conn.fetchrow(
            "SELECT edge_type, decided_by FROM edges WHERE source_id=$1 AND target_id=$2",
            new_id, old_id,
        )
        assert edge["edge_type"] == "tension_with"
        assert edge["decided_by"] == "legacy_lifecycle"


@pytest.mark.asyncio
async def test_writes_narrows_at_or_above_threshold(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        await _insert(conn, agent_id, "Tom uses Mattermost for all communication", "semantic")
        new_id, _ = await _insert(conn, agent_id, "Tom uses Zulip for ops, Mattermost for personal", "semantic")

        payload = {
            "relationship": "narrows",
            "confidence": 0.70,
            "reasoning": "qualifies",
            "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1},
        }
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_returning(payload)):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)

        assert n == 1
        n_narrows = await conn.fetchval(
            "SELECT COUNT(*) FROM edges WHERE edge_type = 'narrows'"
        )
        assert n_narrows == 1


# ── Threshold gating ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_supersedes_below_threshold_no_edge(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        await _insert(conn, agent_id, "Zulip integration is a planned future task", "episodic")
        new_id, _ = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic")

        payload = {
            "relationship": "supersedes", "confidence": 0.70,  # below 0.75
            "reasoning": "low conf",
            "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1},
        }
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_returning(payload)):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)
        assert n == 0
        assert await conn.fetchval("SELECT COUNT(*) FROM edges") == 0


@pytest.mark.asyncio
async def test_independent_no_edge(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        await _insert(conn, agent_id, "Tom is co-founder of Inforge LLC", "semantic")
        new_id, _ = await _insert(conn, agent_id, "Inforge LLC was incorporated in Delaware in March 2023", "semantic")

        payload = {
            "relationship": "independent", "confidence": 0.95,
            "reasoning": "facets",
            "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1},
        }
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_returning(payload)):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)
        assert n == 0


# ── No candidates ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_candidates_skips_llm(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        new_id, _ = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic")
        called = {"n": 0}
        async def _spy(**kw):
            called["n"] += 1
            return None
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_spy):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)
        assert n == 0
        assert called["n"] == 0


# ── Idempotency: no competing edges ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_pair_with_existing_lifecycle_edge_skips_llm(pool, agent_with_address):
    """Per spec: no competing edges. If any lifecycle edge already exists for
    a pair (either direction), skip evaluation entirely — do not call the LLM."""
    from mnemo.server.services import lifecycle_service, atom_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        old_id, old_emb = await _insert(conn, agent_id, "Zulip integration is a planned future task", "episodic")
        new_id, _ = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic",
                                  emb=vector_at_cosine(old_emb, 0.70, seed=3))
        # Pre-existing tension_with edge between the pair (reverse direction).
        await atom_service.create_edge(
            conn=conn, source_id=old_id, target_id=new_id,
            edge_type="tension_with", weight=0.7, metadata={"detector": "manual"},
        )
        called = {"n": 0}
        async def _spy(**kw):
            called["n"] += 1
            return None
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_spy):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)
        assert n == 0
        assert called["n"] == 0


# ── DLQ ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_permanent_failure_writes_dlq(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        cand_id, cand_emb = await _insert(conn, agent_id, "Zulip integration is a planned future task", "episodic")
        new_id, _ = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic",
                                  emb=vector_at_cosine(cand_emb, 0.70, seed=4))

        # _evaluate_pair returns None (permanent failure post-retry).
        async def _eval_none(**kw):
            return None
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_none):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)

        assert n == 0
        dlq = await conn.fetch(
            "SELECT new_atom_id, candidate_id, agent_id FROM lifecycle_dlq"
        )
        assert len(dlq) >= 1
        assert dlq[0]["new_atom_id"] == new_id
        assert dlq[0]["candidate_id"] == cand_id


# ── store_background integration ─────────────────────────────────────────────

async def _run_store(pool, agent, text):
    from mnemo.server.services import store_service
    async with pool.acquire() as conn:
        src = await store_service.create_store(
            conn, agent_id=agent["id"], operator_id=agent["operator"]["id"],
            agent_name=agent["name"], agent_metadata={}, text=text, domain_tags=["t"],
            remembered_on=None, speaker=None, addressee=None, channel=None,
            origin_class="unspecified",
        )
    return await store_service.run_store(pool, src)


@pytest.mark.asyncio
async def test_store_invokes_lifecycle_when_enabled(pool, agent_with_address, monkeypatch):
    from mnemo.server.config import settings
    from mnemo.server.services import lifecycle_service

    monkeypatch.setattr(settings, "lifecycle_detection_enabled", True)
    captured = []

    async def _spy(db, agent, new_id):
        # Store path passes the pool, so no connection is held during the LLM call.
        assert isinstance(db, asyncpg.Pool)
        captured.append(new_id)
        return 0

    with patch.object(lifecycle_service, "detect_lifecycle_relationships", side_effect=_spy):
        outcome = await _run_store(pool, agent_with_address, "The sky over Boston was clear on April 26 2026.")

    assert outcome["status"] == "complete"
    assert len(captured) >= 1


@pytest.mark.asyncio
async def test_store_skips_lifecycle_when_disabled(pool, agent_with_address, monkeypatch):
    from mnemo.server.config import settings
    from mnemo.server.services import lifecycle_service

    monkeypatch.setattr(settings, "lifecycle_detection_enabled", False)
    called = {"n": 0}

    async def _spy(db, agent, new_id):
        called["n"] += 1
        return 0

    with patch.object(lifecycle_service, "detect_lifecycle_relationships", side_effect=_spy):
        await _run_store(pool, agent_with_address, "Random observation about the weather.")

    assert called["n"] == 0


@pytest.mark.asyncio
async def test_detect_with_pool_releases_connection_during_llm(pool, agent_with_address):
    """With a pool, lifecycle detection holds no connection while the model runs."""
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        old_id, old_emb = await _insert(conn, agent_id, "Zulip integration is a planned future task", "episodic")
        new_id, _ = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic",
                                  emb=vector_at_cosine(old_emb, 0.70, seed=6))

    seen = {}

    async def _eval(**kw):
        seen["idle"] = pool.get_idle_size()
        seen["size"] = pool.get_size()
        return {"relationship": "supersedes", "confidence": 0.9, "reasoning": "done",
                "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1}}

    with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval):
        n = await lifecycle_service.detect_lifecycle_relationships(pool, agent_id, new_id)

    assert n == 1
    assert seen["idle"] == seen["size"]


# ── Same-source exclusion and concurrency (post-G2 fixes) ──────────────────

async def _source(conn, agent_id):
    import uuid
    return await conn.fetchval(
        "INSERT INTO sources (agent_id, store_id, text, speaker) VALUES ($1, $2, 'synthetic note', 'tester') RETURNING id",
        agent_id, uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_get_candidates_excludes_same_source(pool, agent_with_address):
    from mnemo.server.services.lifecycle_service import _get_candidates

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        src = await _source(conn, agent_id)
        other_src = await _source(conn, agent_id)
        new_id, new_emb = await _insert(conn, agent_id, "Heron review moves to the week of 30 November", "episodic")
        sib_id, _ = await _insert(conn, agent_id, "Heron runbook must be ready before the review", "episodic",
                                  emb=vector_at_cosine(new_emb, 0.70, seed=11))
        other_id, _ = await _insert(conn, agent_id, "Heron review was planned for mid November", "episodic",
                                    emb=vector_at_cosine(new_emb, 0.70, seed=12))
        legacy_id, _ = await _insert(conn, agent_id, "Heron review needs a room", "episodic",
                                     emb=vector_at_cosine(new_emb, 0.70, seed=13))
        await conn.execute("UPDATE atoms SET source_id = $1 WHERE id = ANY($2)", src, [new_id, sib_id])
        await conn.execute("UPDATE atoms SET source_id = $1 WHERE id = $2", other_src, other_id)

        with_src = {c["id"] for c in await _get_candidates(conn, agent_id, new_id, new_emb, src)}
        without_src = {c["id"] for c in await _get_candidates(conn, agent_id, new_id, new_emb, None)}

    assert sib_id not in with_src
    assert {other_id, legacy_id} <= with_src
    # No source on the new atom (pre-Phase-1 path): nothing extra is excluded.
    assert {sib_id, other_id, legacy_id} <= without_src


@pytest.mark.asyncio
async def test_detect_never_compares_atoms_of_the_same_note(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        src = await _source(conn, agent_id)
        new_id, new_emb = await _insert(conn, agent_id, "Heron review moves to the week of 30 November", "episodic")
        sib_id, _ = await _insert(conn, agent_id, "Heron runbook must be ready before the review", "episodic",
                                  emb=vector_at_cosine(new_emb, 0.70, seed=14))
        old_id, _ = await _insert(conn, agent_id, "Heron review was planned for mid November", "episodic",
                                  emb=vector_at_cosine(new_emb, 0.70, seed=15))
        await conn.execute("UPDATE atoms SET source_id = $1 WHERE id = ANY($2)", src, [new_id, sib_id])

        seen = []

        async def _eval(**kw):
            seen.append(kw["existing_text"])
            return {"relationship": "narrows", "confidence": 0.95, "reasoning": "r",
                    "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1}}

        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)

        targets = {r["target_id"] for r in await conn.fetch(
            "SELECT target_id FROM edges WHERE source_id = $1 AND edge_type = 'narrows'", new_id)}

    assert n == 1
    assert targets == {old_id}
    assert seen == ["Heron review was planned for mid November"]


async def _many_candidates(conn, agent_id, k, seed0):
    new_id, new_emb = await _insert(conn, agent_id, "Kestrel export now runs nightly", "episodic")
    olds = []
    for i in range(k):
        oid, _ = await _insert(conn, agent_id, f"Kestrel export note {i}", "episodic",
                               emb=vector_at_cosine(new_emb, 0.70, seed=seed0 + i))
        olds.append(oid)
    return new_id, olds


@pytest.mark.asyncio
@pytest.mark.parametrize("use_pool", [False, True])
async def test_pairs_are_classified_concurrently_with_a_bound(pool, agent_with_address, monkeypatch, use_pool):
    import asyncio
    from mnemo.server.config import settings
    from mnemo.server.services import lifecycle_service

    monkeypatch.setattr(settings, "lifecycle_candidate_limit", 6)
    monkeypatch.setattr(settings, "lifecycle_max_concurrency", 4)
    agent_id = agent_with_address["id"]
    state = {"now": 0, "max": 0}

    async def _eval(**kw):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.05)
        state["now"] -= 1
        return {"relationship": "tension_with", "confidence": 0.9, "reasoning": "r",
                "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1}}

    async with pool.acquire() as conn:
        new_id, olds = await _many_candidates(conn, agent_id, 6, seed0=20)
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval):
            if use_pool:
                n = await lifecycle_service.detect_lifecycle_relationships(pool, agent_id, new_id)
            else:
                # A caller-held connection: writes must stay sequential.
                n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)
        written = await conn.fetchval(
            "SELECT count(*) FROM edges WHERE source_id = $1 AND edge_type = 'tension_with'", new_id)

    assert n == 6 and written == 6
    assert state["max"] == 4


@pytest.mark.asyncio
async def test_one_failing_pair_does_not_block_the_others(pool, agent_with_address, monkeypatch):
    from mnemo.server.config import settings
    from mnemo.server.services import lifecycle_service

    monkeypatch.setattr(settings, "lifecycle_candidate_limit", 3)
    agent_id = agent_with_address["id"]

    async def _eval(**kw):
        if kw["existing_text"].endswith("1"):
            raise RuntimeError("boom")
        if kw["existing_text"].endswith("2"):
            return None  # permanent failure → DLQ
        return {"relationship": "narrows", "confidence": 0.9, "reasoning": "r",
                "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1}}

    async with pool.acquire() as conn:
        new_id, olds = await _many_candidates(conn, agent_id, 3, seed0=40)
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)
        dlq = await conn.fetchval("SELECT count(*) FROM lifecycle_dlq WHERE new_atom_id = $1", new_id)

    assert n == 1
    assert dlq == 1
