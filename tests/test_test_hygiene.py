"""v2 Phase 1 item 1: the unit suite is offline and deterministic."""

import os

import numpy as np
import pytest

from mnemo.server.decomposers import fake as fake_decomposer
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
    from mnemo.server.decomposers import DecomposeRequest, DecomposerError, decompose_note

    req = lambda t: DecomposeRequest(text=t, agent_name="a", speaker="a")
    atom = DecomposedAtom(text="Scripted.", atom_type="semantic",
                          confidence_alpha=8.0, confidence_beta=1.0)
    fake_decomposer.script([atom], RuntimeError("provider down"))
    first = await decompose_note(req("anything"))
    assert [a.text for a in first.atoms] == ["Scripted."]
    # Unexpected errors surface as a plain DecomposerError.
    with pytest.raises(DecomposerError, match="decomposer error"):
        await decompose_note(req("anything"))
    # Nothing queued: deterministic regex.
    third = await decompose_note(req("Postgres supports JSONB columns."))
    assert third.atoms


# ── Startup guard: fake backends only against a test database ──────────────

PROD_LIKE_URL = "postgresql://mnemo@localhost:5432/mnemo"


@pytest.mark.parametrize("overrides", [
    {"embedding_backend": "fake"},
    {"decomposer_provider": "fake"},
    {"embedding_backend": "fake", "decomposer_provider": "fake"},
])
def test_fake_backends_refused_without_test_db(overrides):
    from pydantic import ValidationError
    from mnemo.server.config import Settings

    with pytest.raises(ValidationError, match="only allowed when MNEMO_DATABASE_URL"):
        Settings(database_url=PROD_LIKE_URL, **overrides)


def test_real_backends_allowed_without_test_db():
    from mnemo.server.config import Settings

    s = Settings(database_url=PROD_LIKE_URL,
                 embedding_backend="sentence_transformers", decomposer_provider="anthropic")
    assert s.embedding_backend == "sentence_transformers"


def test_fake_backends_allowed_with_test_db():
    from mnemo.server.config import Settings

    s = Settings(database_url="postgresql://mnemo@localhost:5433/mnemo_test",
                 embedding_backend="fake", decomposer_provider="fake")
    assert s.decomposer_provider == "fake"


def test_server_process_refuses_to_start_with_fake_backend(tmp_path):
    """End to end: importing the app (what uvicorn does) fails."""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("MNEMO_")}
    env.update({
        "MNEMO_DATABASE_URL": PROD_LIKE_URL,
        "MNEMO_EMBEDDING_BACKEND": "fake",
        "MNEMO_ENV_FILE": str(tmp_path / "none.env"),
    })
    proc = subprocess.run(
        [sys.executable, "-c", "import mnemo.server.main"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0
    assert "only allowed when MNEMO_DATABASE_URL" in proc.stderr
