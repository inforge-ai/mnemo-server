import pytest


async def test_consolidated_atom_ranks_below_specific_atom(client, agent, pool):
    """A consolidated atom should rank below a decomposer atom due to specificity penalty."""
    agent_id = agent["id"]

    # Store a specific fact
    await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "The Hetzner server runs PostgreSQL 16 with pgvector."},
    )

    # Manually insert a consolidated atom (broad semantics)
    from mnemo.server.embeddings import encode

    broad_text = "Generalised from 5 observations: The server infrastructure uses various database technologies."
    embedding = await encode(broad_text)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO atoms (
                agent_id, atom_type, text_content, embedding,
                confidence_alpha, confidence_beta,
                source_type, domain_tags,
                decay_half_life_days, decay_type, decomposer_version
            ) VALUES ($1, 'semantic', $2, $3::vector, 8.0, 1.0,
                      'consolidation', '{}', 90.0, 'none', 'consolidation_v1')
            """,
            agent_id,
            broad_text,
            embedding,
        )

    # Recall -- the specific atom should rank above the consolidated one
    resp = await client.post(
        f"/v1/agents/{agent_id}/recall",
        json={"query": "PostgreSQL database server", "max_results": 10},
    )
    assert resp.status_code == 200
    atoms = resp.json()["atoms"]
    specific = [a for a in atoms if "Hetzner" in a["text_content"]]
    consolidated = [a for a in atoms if "Generalised" in a["text_content"]]

    if specific and consolidated:
        specific_score = specific[0]["relevance_score"]
        consolidated_score = consolidated[0]["relevance_score"]
        assert consolidated_score < specific_score, (
            f"Consolidated atom ({consolidated_score:.3f}) should score lower "
            f"than specific atom ({specific_score:.3f})"
        )


async def test_composite_score_penalty_applied():
    """Unit test: composite_score applies 15% penalty to consolidation atoms."""
    from mnemo.server.services.atom_service import composite_score

    sim, conf = 0.8, 0.9
    normal = composite_score(sim, conf, "direct_experience")
    penalised = composite_score(sim, conf, "consolidation")

    assert penalised == pytest.approx(normal * 0.85, rel=1e-6)
    assert penalised < normal
