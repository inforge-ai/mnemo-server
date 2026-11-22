"""Shared test helpers."""

import numpy as np


def vector_at_cosine(base: list[float], cos: float, seed: int = 0) -> list[float]:
    """Unit vector with exactly `cos` cosine similarity to `base`.

    The fake embedder (MNEMO_EMBEDDING_BACKEND=fake) is lexical, so tests that
    need a specific semantic relationship (a lifecycle-band pair, a near
    duplicate) construct the embedding directly instead of relying on wording.
    """
    b = np.asarray(base, dtype=np.float64)
    b = b / np.linalg.norm(b)
    rng = np.random.default_rng(seed)
    r = rng.standard_normal(b.shape[0])
    u = r - r.dot(b) * b
    u = u / np.linalg.norm(u)
    v = cos * b + np.sqrt(max(0.0, 1.0 - cos * cos)) * u
    return (v / np.linalg.norm(v)).tolist()


async def store_text(conn, agent_id, text: str, domain_tags=None, agent_name: str = "test-agent") -> dict:
    """Decompose + persist a note directly (no route, no source row) in one
    transaction on `conn`. Mirrors store_service.run_store's two phases."""
    from mnemo.server.decomposers import DecomposeRequest
    from mnemo.server.services import atom_service

    prepared = await atom_service.prepare_atoms(
        DecomposeRequest(text=text, agent_name=agent_name, speaker=agent_name)
    )
    async with conn.transaction():
        return await atom_service.persist_atoms(conn, agent_id, prepared, list(domain_tags or []))
