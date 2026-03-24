import pytest


async def test_recall_full_verbosity_includes_confidence_metadata(client, agent):
    """At verbosity=full, recall output should include alpha, beta."""
    agent_id = agent["id"]
    await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "The project deadline is June 15."},
    )

    resp = await client.post(
        f"/v1/agents/{agent_id}/recall",
        json={"query": "project deadline", "verbosity": "full"},
    )
    assert resp.status_code == 200
    atoms = resp.json()["atoms"]
    assert len(atoms) >= 1
    atom = atoms[0]
    assert "confidence_alpha" in atom
    assert "confidence_beta" in atom
    assert atom["confidence_alpha"] > 0
    assert atom["confidence_beta"] > 0


async def test_recall_summary_verbosity_excludes_confidence_metadata(client, agent):
    """At verbosity=summary, alpha/beta should NOT be in the response."""
    agent_id = agent["id"]
    await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "The project deadline is June 15."},
    )

    resp = await client.post(
        f"/v1/agents/{agent_id}/recall",
        json={"query": "project deadline", "verbosity": "summary"},
    )
    assert resp.status_code == 200
    atoms = resp.json()["atoms"]
    assert len(atoms) >= 1
    atom = atoms[0]
    assert "confidence_alpha" not in atom
    assert "confidence_beta" not in atom
