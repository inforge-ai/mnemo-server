import pytest


async def test_bayesian_alpha_increments_on_duplicate_store(client, agent, pool):
    """Storing the same fact multiple times should increment alpha via Bayesian update."""
    agent_id = agent["id"]
    text = "The sky is blue."

    # Store the fact 3 times
    for _ in range(3):
        resp = await client.post(
            f"/v1/agents/{agent_id}/remember",
            json={"text": text},
        )
        assert resp.status_code == 201

    # Query the atoms table directly to check alpha
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT confidence_alpha, confidence_beta, access_count
            FROM atoms
            WHERE agent_id = $1 AND text_content LIKE '%sky is blue%'
            AND is_active = true
            ORDER BY confidence_alpha DESC
            LIMIT 1
            """,
            agent_id,
        )

    assert row is not None, "Atom not found"
    assert row["confidence_alpha"] > 2.0, f"Alpha not incremented: {row['confidence_alpha']}"
    assert row["access_count"] >= 1, f"Access count not incremented: {row['access_count']}"


async def test_bayesian_update_persists_to_database(client, agent, pool):
    """Verify the Bayesian update is persisted, not just in-memory."""
    agent_id = agent["id"]
    text = "Water boils at 100 degrees Celsius."

    # Store twice
    await client.post(f"/v1/agents/{agent_id}/remember", json={"text": text})
    await client.post(f"/v1/agents/{agent_id}/remember", json={"text": text})

    # Read directly from DB
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT confidence_alpha
            FROM atoms
            WHERE agent_id = $1 AND text_content LIKE '%boils at 100%'
            AND is_active = true
            ORDER BY confidence_alpha DESC
            LIMIT 1
            """,
            agent_id,
        )

    assert row is not None
    initial_alpha = row["confidence_alpha"]

    # Store a third time
    await client.post(f"/v1/agents/{agent_id}/remember", json={"text": text})

    async with pool.acquire() as conn:
        row2 = await conn.fetchrow(
            """
            SELECT confidence_alpha
            FROM atoms
            WHERE agent_id = $1 AND text_content LIKE '%boils at 100%'
            AND is_active = true
            ORDER BY confidence_alpha DESC
            LIMIT 1
            """,
            agent_id,
        )

    assert row2["confidence_alpha"] >= initial_alpha
