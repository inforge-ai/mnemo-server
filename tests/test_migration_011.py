"""v2 Phase 2: migration 011 (kind, validity, status sync, relation tables)."""

from uuid import uuid4

import asyncpg
import pytest

from mnemo.server.embeddings import encode
from tests.test_consolidation import _insert_atom_sql


async def _atom(conn, agent_id, text="Synthetic atom for migration 011."):
    return await _insert_atom_sql(conn, agent_id, "semantic", text, await encode(text))


async def test_011_recorded(pool):
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version = '011_relations'") == 1


async def test_new_atom_defaults(pool, agent):
    async with pool.acquire() as conn:
        aid = await _atom(conn, agent["id"])
        row = await conn.fetchrow(
            "SELECT kind, kind_p, status, valid_from, valid_until, date_precision, date_basis FROM atoms WHERE id = $1", aid)
    assert dict(row) == {"kind": "unknown", "kind_p": None, "status": "active", "valid_from": None,
                         "valid_until": None, "date_precision": "unknown", "date_basis": None}


@pytest.mark.parametrize("col, bad", [
    ("kind", "'habit'"), ("status", "'deleted'"), ("date_precision", "'week'"),
    ("date_basis", "'guessed'"), ("kind_p", "1.5"),
])
async def test_checks(pool, agent, col, bad):
    async with pool.acquire() as conn:
        aid = await _atom(conn, agent["id"])
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(f"UPDATE atoms SET {col} = {bad} WHERE id = $1", aid)


async def test_status_follows_is_active(pool, agent):
    async with pool.acquire() as conn:
        aid = await _atom(conn, agent["id"])
        await conn.execute("UPDATE atoms SET is_active = false WHERE id = $1", aid)
        assert await conn.fetchval("SELECT status FROM atoms WHERE id = $1", aid) == "archived"
        await conn.execute("UPDATE atoms SET is_active = true WHERE id = $1", aid)
        assert await conn.fetchval("SELECT status FROM atoms WHERE id = $1", aid) == "active"


async def test_is_active_follows_status(pool, agent):
    async with pool.acquire() as conn:
        aid = await _atom(conn, agent["id"])
        for status, active in [("superseded", True), ("merged", True), ("archived", False), ("active", True)]:
            await conn.execute("UPDATE atoms SET status = $2 WHERE id = $1", aid, status)
            assert await conn.fetchval("SELECT is_active FROM atoms WHERE id = $1", aid) is active, status


async def test_reactivating_a_superseded_atom_keeps_its_status(pool, agent):
    """is_active true -> false -> true on a superseded atom: archived, then active."""
    async with pool.acquire() as conn:
        aid = await _atom(conn, agent["id"])
        await conn.execute("UPDATE atoms SET status = 'superseded' WHERE id = $1", aid)
        await conn.execute("UPDATE atoms SET is_active = true WHERE id = $1", aid)   # no change
        assert await conn.fetchval("SELECT status FROM atoms WHERE id = $1", aid) == "superseded"


async def test_insert_trigger(pool, agent):
    """BEFORE INSERT: an inactive insert is archived; an archived insert is inactive."""
    async with pool.acquire() as conn:
        emb = await encode("x")
        a = await conn.fetchval(
            "INSERT INTO atoms (agent_id, atom_type, text_content, embedding, confidence_alpha, confidence_beta, is_active) "
            "VALUES ($1, 'semantic', 'inactive insert', $2, 2, 2, false) RETURNING id", agent["id"], emb)
        b = await conn.fetchval(
            "INSERT INTO atoms (agent_id, atom_type, text_content, embedding, confidence_alpha, confidence_beta, status) "
            "VALUES ($1, 'semantic', 'archived insert', $2, 2, 2, 'archived') RETURNING id", agent["id"], emb)
        assert await conn.fetchval("SELECT status FROM atoms WHERE id = $1", a) == "archived"
        assert await conn.fetchval("SELECT is_active FROM atoms WHERE id = $1", b) is False


async def test_no_row_breaks_the_status_invariant(pool, agent):
    async with pool.acquire() as conn:
        await _atom(conn, agent["id"])
        assert await conn.fetchval(
            "SELECT count(*) FROM atoms WHERE (status = 'archived') <> (NOT is_active)") == 0


async def test_new_edge_types_and_decided_by(pool, agent):
    async with pool.acquire() as conn:
        a, b = await _atom(conn, agent["id"], "atom a"), await _atom(conn, agent["id"], "atom b")
        for t in ("same", "replaces", "refines", "conflict", "review"):
            await conn.execute("INSERT INTO edges (source_id, target_id, edge_type, weight, decided_by) "
                               "VALUES ($1, $2, $3, 0.9, 'relation_v2')", a, b, t)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO edges (source_id, target_id, edge_type, weight) VALUES ($1, $2, 'bogus', 1)", a, b)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO edges (source_id, target_id, edge_type, weight, decided_by) "
                               "VALUES ($1, $2, 'related', 1, 'oracle')", a, b)


async def test_relation_decisions_and_conflicts(pool, agent):
    async with pool.acquire() as conn:
        a, b = await _atom(conn, agent["id"], "atom a"), await _atom(conn, agent["id"], "atom b")
        rid = await conn.fetchval(
            "INSERT INTO relation_decisions (agent_id, new_atom_id, old_atom_id, final_relation, mode, decided_by) "
            "VALUES ($1, $2, $3, 'pending', 'shadow', 'relation_v2') RETURNING id", agent["id"], a, b)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO relation_decisions (agent_id, old_atom_id, final_relation, mode, decided_by) "
                "VALUES ($1, $2, 'maybe', 'shadow', 'relation_v2')", agent["id"], b)
        await conn.execute("INSERT INTO conflicts (agent_id, atom_a, atom_b, decision_id) VALUES ($1, $2, $3, $4)",
                           agent["id"], a, b, rid)
        with pytest.raises(asyncpg.UniqueViolationError):   # one open conflict per unordered pair
            await conn.execute("INSERT INTO conflicts (agent_id, atom_a, atom_b) VALUES ($1, $2, $3)", agent["id"], b, a)
        await conn.execute("UPDATE conflicts SET resolved_at = now() WHERE atom_a = $1", a)
        await conn.execute("INSERT INTO conflicts (agent_id, atom_a, atom_b) VALUES ($1, $2, $3)", agent["id"], b, a)
