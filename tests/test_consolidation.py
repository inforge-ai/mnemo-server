"""
Integration tests for the consolidation service.

Strategy:
- Use the pool fixture to acquire DB connections directly when we need to
  bypass normal API dedup logic (e.g. inserting atoms with identical embeddings).
- Run run_consolidation(pool) and check return counts + DB state.
"""

import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from uuid import UUID

from mnemo.server.services.consolidation import run_consolidation, _CONSOLIDATION_LOCK_KEY
from mnemo.server.embeddings import encode


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _insert_atom_sql(
    conn,
    agent_id: UUID,
    atom_type: str,
    text: str,
    embedding: list[float],
    *,
    confidence_alpha: float = 4.0,
    confidence_beta: float = 2.0,
    source_type: str = "direct_experience",
    domain_tags: list[str] | None = None,
    decay_type: str = "exponential",
    half_life: float = 30.0,
) -> UUID:
    """Insert an atom directly via SQL, bypassing API dedup."""
    row = await conn.fetchrow(
        """
        INSERT INTO atoms (
            agent_id, atom_type, text_content, embedding,
            confidence_alpha, confidence_beta, source_type,
            domain_tags, decay_type, decay_half_life_days
        ) VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8, $9, $10)
        RETURNING id
        """,
        agent_id,
        atom_type,
        text,
        embedding,
        confidence_alpha,
        confidence_beta,
        source_type,
        domain_tags or ["test"],
        decay_type,
        half_life,
    )
    return row["id"]


# ── Decay tests ────────────────────────────────────────────────────────────────

async def test_decay_deactivates_old_atoms(client, agent, pool):
    """Atoms whose effective_confidence drops below 0.05 are deactivated."""
    # Store an atom via the API (normal path, gets episodic half-life 14d)
    r = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={
            "text": "I found a memory leak in the authentication service.",
            "domain_tags": ["bugs"],
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["atoms_created"] >= 1

    agent_id = UUID(agent["id"])

    # Artificially age all atoms so effective_confidence << 0.05
    # With episodic half_life=14d and exponential decay:
    # eff_conf = base * 0.5^(age/14) → after 365 days ≈ 0.889 * 0.5^26 ≈ 1e-8
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE atoms
            SET created_at = now() - interval '365 days',
                last_accessed = NULL
            WHERE agent_id = $1
            """,
            agent_id,
        )

    result = await run_consolidation(pool)

    assert result["decayed"] >= 1

    # Verify via stats endpoint
    stats_r = await client.get(f"/v1/agents/{agent['id']}/stats")
    assert stats_r.status_code == 200
    assert stats_r.json()["active_atoms"] == 0


async def test_decay_does_not_touch_fresh_atoms(client, agent, pool):
    """Freshly created atoms should not be deactivated by consolidation."""
    r = await client.post(
        f"/v1/agents/{agent['id']}/remember",
        json={"text": "Python dict comprehensions are very efficient.", "domain_tags": []},
    )
    assert r.status_code == 201

    result = await run_consolidation(pool)

    # None of the fresh atoms should have been deactivated
    stats_r = await client.get(f"/v1/agents/{agent['id']}/stats")
    assert stats_r.json()["active_atoms"] > 0
    # decayed could be 0 or include atoms from other agents/runs — just check ours
    assert stats_r.json()["active_atoms"] >= r.json()["atoms_created"]


# ── Cluster / generalise tests ─────────────────────────────────────────────────

async def test_cluster_creates_generalised_atom(client, agent, pool):
    """
    Three or more episodic atoms with identical embeddings (cosine = 1.0 > 0.85)
    should produce one generalised semantic atom with 'generalises' edges.
    """
    agent_id = UUID(agent["id"])
    emb = await encode("I discovered async issues in the codebase.")

    async with pool.acquire() as conn:
        for i in range(3):
            await _insert_atom_sql(
                conn,
                agent_id,
                "episodic",
                f"I discovered async issue #{i} while debugging.",
                emb,
                confidence_alpha=8.0,
                confidence_beta=1.0,
                domain_tags=["python", "async"],
                decay_type="none",
                half_life=14.0,
            )

    result = await run_consolidation(pool)

    assert result["clustered"] >= 1

    # Verify the generalised semantic atom exists
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS cnt
            FROM atoms
            WHERE agent_id = $1
              AND atom_type = 'semantic'
              AND source_type = 'consolidation'
              AND is_active = true
            """,
            agent_id,
        )
    assert row["cnt"] >= 1

    # Verify 'generalises' edges exist pointing to the episodic atoms
    async with pool.acquire() as conn:
        gen_ids = [
            r["id"] for r in await conn.fetch(
                """
                SELECT id FROM atoms
                WHERE agent_id = $1 AND source_type = 'consolidation' AND is_active = true
                """,
                agent_id,
            )
        ]
        assert len(gen_ids) >= 1
        edge_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM edges
            WHERE source_id = ANY($1) AND edge_type = 'generalises'
            """,
            gen_ids,
        )
    # After the merge step consolidates the identical-embedding episodic atoms
    # into one survivor, some 'generalises' edges get reassigned.
    # At minimum, one edge to the surviving atom must exist.
    assert edge_count >= 1


async def test_cluster_requires_three_atoms(client, agent, pool):
    """Two similar episodic atoms are NOT enough to trigger generalisation."""
    agent_id = UUID(agent["id"])
    emb = await encode("I found a bug in the scheduler.")

    async with pool.acquire() as conn:
        for i in range(2):
            await _insert_atom_sql(
                conn,
                agent_id,
                "episodic",
                f"I found scheduler bug #{i}.",
                emb,
                domain_tags=["scheduler"],
                decay_type="none",
                half_life=14.0,
            )

    result = await run_consolidation(pool)
    assert result["clustered"] == 0


async def test_already_generalised_atoms_are_skipped(client, agent, pool):
    """A second consolidation run should NOT create duplicate generalised atoms."""
    agent_id = UUID(agent["id"])
    emb = await encode("I encountered a memory leak in the service.")

    async with pool.acquire() as conn:
        for i in range(3):
            await _insert_atom_sql(
                conn,
                agent_id,
                "episodic",
                f"I encountered memory leak #{i} today.",
                emb,
                domain_tags=["memory"],
                decay_type="none",
                half_life=14.0,
            )

    r1 = await run_consolidation(pool)
    assert r1["clustered"] >= 1

    r2 = await run_consolidation(pool)
    # The already-generalised atoms should be excluded; no new cluster created
    assert r2["clustered"] == 0


# ── Merge duplicate tests ──────────────────────────────────────────────────────

async def test_merge_duplicates_combines_atoms(client, agent, pool):
    """
    Two active atoms with the same embedding (cosine 1.0 > 0.90), same agent,
    same type → older atom absorbs the newer and newer is deactivated.
    Merge is recorded in access_log, NOT as a graph edge.
    """
    agent_id = UUID(agent["id"])
    emb = await encode("Python dictionaries maintain insertion order since 3.7.")

    async with pool.acquire() as conn:
        id_old = await _insert_atom_sql(
            conn,
            agent_id,
            "semantic",
            "Python dicts maintain insertion order.",
            emb,
            confidence_alpha=4.0,
            confidence_beta=2.0,
            domain_tags=["python"],
            decay_type="none",
            half_life=90.0,
        )
        # Ensure id_old is truly the older atom by tweaking created_at
        await conn.execute(
            "UPDATE atoms SET created_at = now() - interval '1 hour' WHERE id = $1",
            id_old,
        )
        id_new = await _insert_atom_sql(
            conn,
            agent_id,
            "semantic",
            "Dictionaries in Python preserve insertion order since version 3.7.",
            emb,
            confidence_alpha=4.0,
            confidence_beta=2.0,
            domain_tags=["python"],
            decay_type="none",
            half_life=90.0,
        )

    result = await run_consolidation(pool)
    assert result["merged"] >= 1

    async with pool.acquire() as conn:
        # Older atom should still be active
        old_row = await conn.fetchrow(
            "SELECT is_active, confidence_alpha FROM atoms WHERE id = $1", id_old
        )
        assert old_row["is_active"] is True
        # Confidence should have grown (Bayesian merge: α = 4+4-1 = 7)
        assert old_row["confidence_alpha"] > 4.0

        # Newer atom should be deactivated
        new_row = await conn.fetchrow("SELECT is_active FROM atoms WHERE id = $1", id_new)
        assert new_row["is_active"] is False

        # No 'generalises' edge should exist between the two atoms (Fix 3)
        edge_count = await conn.fetchval(
            "SELECT COUNT(*) FROM edges WHERE source_id = $1 AND target_id = $2",
            id_old, id_new,
        )
        assert edge_count == 0

        # Merge should be recorded in access_log
        log_row = await conn.fetchrow(
            "SELECT metadata FROM access_log WHERE action = 'merge' AND target_id = $1",
            id_old,
        )
        assert log_row is not None
        import json as _json
        meta = _json.loads(log_row["metadata"])
        assert meta["absorbed_atom_id"] == str(id_new)


async def test_merge_does_not_merge_different_types(client, agent, pool):
    """Atoms of different types (episodic vs semantic) must not be merged."""
    agent_id = UUID(agent["id"])
    emb = await encode("The service crashed under load.")

    async with pool.acquire() as conn:
        await _insert_atom_sql(
            conn, agent_id, "episodic", "I saw the service crash under load.",
            emb, domain_tags=["ops"], decay_type="none", half_life=14.0,
        )
        await _insert_atom_sql(
            conn, agent_id, "semantic", "Services crash under high load without backpressure.",
            emb, domain_tags=["ops"], decay_type="none", half_life=90.0,
        )

    result = await run_consolidation(pool)
    # No merge should have happened (different atom_type)
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM atoms WHERE agent_id = $1 AND is_active = true",
            agent_id,
        )
    assert count == 2


# ── Departed agent cleanup tests ───────────────────────────────────────────────

async def test_purge_deletes_expired_departed_agents(client, pool):
    """Agents with data_expires_at in the past should be fully deleted."""
    r = await client.post(
        "/v1/agents",
        json={"name": "departed-agent", "domain_tags": ["test"]},
    )
    assert r.status_code == 201
    dep_id = UUID(r.json()["id"])

    # Simulate departure with expired retention window
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agents
            SET is_active = false,
                departed_at = now() - interval '31 days',
                data_expires_at = now() - interval '1 day'
            WHERE id = $1
            """,
            dep_id,
        )

    result = await run_consolidation(pool)
    assert result["purged"] >= 1

    # Verify the agent is gone
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agents WHERE id = $1", dep_id)
    assert row is None


async def test_purge_keeps_agents_with_future_expiry(client, pool):
    """Agents whose data_expires_at is in the future must not be deleted."""
    r = await client.post(
        "/v1/agents",
        json={"name": "recent-departure", "domain_tags": ["test"]},
    )
    assert r.status_code == 201
    dep_id = UUID(r.json()["id"])

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agents
            SET is_active = false,
                departed_at = now() - interval '1 day',
                data_expires_at = now() + interval '29 days'
            WHERE id = $1
            """,
            dep_id,
        )

    result = await run_consolidation(pool)

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM agents WHERE id = $1", dep_id)
    assert row is not None  # should still exist


async def test_purge_removes_capability_references(client, two_agents, pool):
    """
    Purging a departed agent must also clean up capability rows where the agent
    appears as grantee (FK constraint has no CASCADE).
    """
    alice, bob = two_agents

    # Alice stores something and creates a view
    await client.post(
        f"/v1/agents/{alice['id']}/remember",
        json={"text": "pandas handles missing values automatically.", "domain_tags": ["data"]},
    )
    view_r = await client.post(
        f"/v1/agents/{alice['id']}/views",
        json={"name": "alice-view", "atom_filter": {"atom_types": ["semantic"]}},
    )
    assert view_r.status_code == 201
    view_id = view_r.json()["id"]

    # Alice grants Bob access
    grant_r = await client.post(
        f"/v1/agents/{alice['id']}/grant",
        json={"view_id": view_id, "grantee_id": bob["id"], "permissions": ["read"]},
    )
    assert grant_r.status_code == 201

    # Mark Bob as departed with expired data
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agents
            SET is_active = false,
                departed_at = now() - interval '31 days',
                data_expires_at = now() - interval '1 day'
            WHERE id = $1
            """,
            UUID(bob["id"]),
        )

    # Purge should succeed without FK violation
    result = await run_consolidation(pool)
    assert result["purged"] >= 1

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM agents WHERE id = $1", UUID(bob["id"]))
    assert row is None


# ── Audit log test ─────────────────────────────────────────────────────────────

async def test_consolidation_writes_audit_log(client, agent, pool):
    """Each consolidation run should append a record to access_log."""
    async with pool.acquire() as conn:
        before_count = await conn.fetchval(
            "SELECT COUNT(*) FROM access_log WHERE action = 'consolidation'"
        )

    await run_consolidation(pool)

    async with pool.acquire() as conn:
        after_count = await conn.fetchval(
            "SELECT COUNT(*) FROM access_log WHERE action = 'consolidation'"
        )

    assert after_count == before_count + 1


# ── Advisory lock tests ────────────────────────────────────────────────────────

async def test_consolidation_advisory_lock(pool):
    """Second consolidation run is skipped when the advisory lock is already held."""
    async with pool.acquire() as lock_conn:
        # Acquire the session-level advisory lock on a separate connection
        await lock_conn.execute("SELECT pg_advisory_lock($1)", _CONSOLIDATION_LOCK_KEY)
        try:
            # run_consolidation will try pg_try_advisory_lock on a different connection
            # and find the lock held — should return immediately with skipped=True
            result = await run_consolidation(pool)
            assert result.get("skipped") is True
            assert result["merged"] == 0
            assert result["decayed"] == 0
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock($1)", _CONSOLIDATION_LOCK_KEY)


async def test_consolidation_step_rollback(client, agent, pool):
    """
    If a step fails, its transaction is rolled back. Prior steps remain committed.
    """
    agent_id = UUID(agent["id"])

    # Store an atom and age it so it gets decayed
    r = await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "The connection pool is exhausted under high load.", "domain_tags": ["ops"]},
    )
    assert r.status_code == 201

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE atoms SET created_at = now() - interval '365 days', last_accessed = NULL WHERE agent_id = $1",
            agent_id,
        )

    # Patch merge step to raise after the decay step has already committed
    with patch(
        "mnemo.server.services.consolidation._merge_duplicates",
        new_callable=AsyncMock,
        side_effect=RuntimeError("injected merge failure"),
    ):
        with pytest.raises(RuntimeError, match="injected merge failure"):
            await run_consolidation(pool)

    # Decay step ran and committed before merge raised — atoms should be inactive
    async with pool.acquire() as conn:
        active = await conn.fetchval(
            "SELECT COUNT(*) FROM atoms WHERE agent_id = $1 AND is_active = true",
            agent_id,
        )
    assert active == 0  # decay committed even though merge failed


# ── Dead edge pruning tests ────────────────────────────────────────────────────

async def test_dead_edge_pruning(client, agent, pool):
    """
    Edges pointing to or from deactivated atoms should be removed by consolidation.
    """
    agent_id = UUID(agent["id"])
    emb_a = await encode("The query planner chooses index scans for selective filters.")
    emb_b = await encode("Vacuuming reclaims dead tuples in PostgreSQL.")
    emb_c = await encode("Connection pooling reduces overhead for short-lived queries.")

    async with pool.acquire() as conn:
        id_a = await _insert_atom_sql(
            conn, agent_id, "semantic", "Query planner uses index scans.",
            emb_a, domain_tags=["postgres"], decay_type="none", half_life=90.0,
        )
        id_b = await _insert_atom_sql(
            conn, agent_id, "semantic", "Vacuuming reclaims dead tuples.",
            emb_b, domain_tags=["postgres"], decay_type="none", half_life=90.0,
        )
        id_c = await _insert_atom_sql(
            conn, agent_id, "semantic", "Connection pooling reduces overhead.",
            emb_c, domain_tags=["postgres"], decay_type="none", half_life=90.0,
        )
        # Create edges: A→B and B→C
        await conn.execute(
            "INSERT INTO edges (source_id, target_id, edge_type, weight) VALUES ($1, $2, 'supports', 1.0)",
            id_a, id_b,
        )
        await conn.execute(
            "INSERT INTO edges (source_id, target_id, edge_type, weight) VALUES ($1, $2, 'supports', 1.0)",
            id_b, id_c,
        )
        # Manually deactivate B
        await conn.execute("UPDATE atoms SET is_active = false WHERE id = $1", id_b)

    result = await run_consolidation(pool)
    assert result["pruned"] >= 2  # A→B and B→C both removed

    async with pool.acquire() as conn:
        # No edges should involve B
        edge_count = await conn.fetchval(
            "SELECT COUNT(*) FROM edges WHERE source_id = $1 OR target_id = $1",
            id_b,
        )
        assert edge_count == 0
        # A and C should still exist and be active
        a_row = await conn.fetchrow("SELECT is_active FROM atoms WHERE id = $1", id_a)
        c_row = await conn.fetchrow("SELECT is_active FROM atoms WHERE id = $1", id_c)
        assert a_row["is_active"] is True
        assert c_row["is_active"] is True
