import math

import pytest

from mnemo.server.services.atom_service import MERGE_CEILING, bayesian_merge_damped


class TestBayesianMergeDamped:
    """Pure-function tests for the damped Bayesian merge used on dedup / consolidation.

    The damping exists because the classical α_new = α_old + α_inc − 1 compounded
    without bound in production — α reached 198,429 on one atom. See
    docs/mnemo-confidence-audit-23042026.md.
    """

    def test_diminishing_returns(self):
        """Each successive merge of identical incoming evidence contributes less α."""
        state = (4.0, 2.0)
        incoming = (8.0, 1.0)
        gains = []
        for _ in range(5):
            new = bayesian_merge_damped(state[0], state[1], *incoming)
            gains.append(new[0] - state[0])
            state = new
        # Strictly decreasing gains
        for i in range(1, len(gains)):
            assert gains[i] < gains[i - 1], (
                f"gain at step {i} ({gains[i]:.4f}) not less than step {i-1} ({gains[i-1]:.4f}); "
                f"gains={gains}"
            )

    def test_ceiling_clamps_alpha(self):
        """No matter how many merges or how large the incoming, α stays ≤ ceiling."""
        α, β = MERGE_CEILING - 0.5, 1.0
        for _ in range(100):
            α, β = bayesian_merge_damped(α, β, 8.0, 1.0)
        assert α <= MERGE_CEILING
        assert α >= MERGE_CEILING - 1.0  # should have hit ceiling quickly

    def test_ceiling_clamps_beta(self):
        """β is clamped symmetrically."""
        α, β = 1.0, MERGE_CEILING - 0.5
        for _ in range(100):
            α, β = bayesian_merge_damped(α, β, 1.0, 8.0)
        assert β <= MERGE_CEILING

    def test_floor_at_one(self):
        """α and β never drop below 1.0 even from degenerate inputs."""
        α, β = bayesian_merge_damped(0.5, 0.5, 0.5, 0.5)
        assert α >= 1.0
        assert β >= 1.0

    def test_symmetry_alpha_beta(self):
        """Swapping α/β inputs produces swapped outputs (the law is symmetric)."""
        α1, β1 = bayesian_merge_damped(4.0, 2.0, 8.0, 1.0)
        β2, α2 = bayesian_merge_damped(2.0, 4.0, 1.0, 8.0)
        assert math.isclose(α1, α2, rel_tol=1e-9)
        assert math.isclose(β1, β2, rel_tol=1e-9)

    def test_incoming_prior_is_near_noop(self):
        """Incoming (1, 1) carries no evidence (it IS the improper prior); α, β unchanged."""
        α, β = bayesian_merge_damped(10.0, 2.0, 1.0, 1.0)
        assert math.isclose(α, 10.0, abs_tol=1e-9)
        assert math.isclose(β, 2.0, abs_tol=1e-9)

    def test_first_merge_moves_alpha_meaningfully(self):
        """Early merges — where damping is gentle — produce a visible gain, not a no-op.
        Regression guard: damping must not be so aggressive it defeats the mechanism."""
        α, β = bayesian_merge_damped(2.0, 2.0, 8.0, 1.0)
        assert α > 4.0, f"first merge from prior barely moved α: {α}"
        assert α < 9.0, f"first merge overshot what the damped law should allow: {α}"

    def test_returns_floats(self):
        α, β = bayesian_merge_damped(4, 2, 8, 1)
        assert isinstance(α, float)
        assert isinstance(β, float)


async def test_bayesian_alpha_increments_on_duplicate_store(client, agent, pool):
    """Storing the same fact multiple times should increment alpha via Bayesian update."""
    agent_id = agent["id"]
    ag_headers = {"X-Agent-Key": agent["agent_key"]}
    text = "The sky is blue."

    # Store the fact 3 times
    for _ in range(3):
        resp = await client.post(
            f"/v1/agents/{agent_id}/remember",
            json={"text": text},
            headers=ag_headers,
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
    assert row["confidence_alpha"] > 4.0, f"Alpha not incremented beyond initial 4.0: {row['confidence_alpha']}"
    assert row["access_count"] >= 1, f"Access count not incremented: {row['access_count']}"


async def test_bayesian_update_persists_to_database(client, agent, pool):
    """Verify the Bayesian update is persisted, not just in-memory."""
    agent_id = agent["id"]
    ag_headers = {"X-Agent-Key": agent["agent_key"]}
    text = "Water boils at 100 degrees Celsius."

    # Store twice
    await client.post(f"/v1/agents/{agent_id}/remember", json={"text": text}, headers=ag_headers)
    await client.post(f"/v1/agents/{agent_id}/remember", json={"text": text}, headers=ag_headers)

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
    await client.post(f"/v1/agents/{agent_id}/remember", json={"text": text}, headers=ag_headers)

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

    assert row2["confidence_alpha"] > initial_alpha
