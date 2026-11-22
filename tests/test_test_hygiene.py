"""v2 Phase 1 item 1: the unit suite is offline and deterministic."""

import os

import numpy as np
import pytest

from mnemo.server import fake_decomposer
from mnemo.server.config import settings
from mnemo.server.decomposer import DecomposedAtom
from mnemo.server.embeddings import encode, encode_batch
from tests.helpers import vector_at_cosine


def test_suite_uses_fake_backends():
    assert settings.embedding_backend == "fake"
    assert settings.decomposer_provider == "fake"
    assert "test" in settings.database_url


def test_live_keys_stripped():
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "OPENROUTER_API_KEY" not in os.environ


async def test_fake_embedder_deterministic_and_graded():
    a = await encode("The deploy window is Tuesday morning.")
    a2 = await encode("The deploy window is Tuesday morning.")
    near = await encode("The deploy window is Tuesday afternoon.")
    far = await encode("Herons nest in tall reeds near the lake.")
    assert a == a2
    assert len(a) == settings.embedding_dim
    assert np.linalg.norm(a) == pytest.approx(1.0)
    sim_near, sim_far = np.dot(a, near), np.dot(a, far)
    assert sim_near > 0.5 > sim_far


async def test_encode_batch_matches_encode():
    texts = ["alpha beta", "gamma delta epsilon", "alpha beta"]
    batch = await encode_batch(texts)
    assert batch == [await encode(t) for t in texts]
    assert await encode_batch([]) == []


def test_vector_at_cosine():
    base = np.random.default_rng(0).standard_normal(16)
    v = vector_at_cosine(base.tolist(), 0.7, seed=1)
    assert np.dot(base / np.linalg.norm(base), v) == pytest.approx(0.7)


async def test_live_anthropic_client_is_refused(request):
    import anthropic
    with pytest.raises(RuntimeError, match="non-eval"):
        anthropic.AsyncAnthropic(api_key="x")
    assert request.node._live_llm_attempts == [request.node.nodeid]
    # Deliberate attempt: clear it so the guard's teardown check passes.
    request.node._live_llm_attempts.clear()


async def test_fake_decomposer_scripts_and_raises():
    from mnemo.server.services.atom_service import _decompose

    atom = DecomposedAtom(text="Scripted.", atom_type="semantic",
                          confidence_alpha=8.0, confidence_beta=1.0)
    fake_decomposer.script([atom], RuntimeError("provider down"))
    first = await _decompose("anything")
    assert [a.text for a in first.atoms] == ["Scripted."]
    with pytest.raises(RuntimeError, match="provider down"):
        await _decompose("anything")
    # Nothing queued: deterministic regex fallback.
    third = await _decompose("Postgres supports JSONB columns.")
    assert third.atoms
