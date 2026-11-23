"""v2 Phase 1: migration 010 (sources, provenance columns, source_merges)."""

from uuid import uuid4

import asyncpg
import pytest

from tests.test_consolidation import _insert_atom_sql
from mnemo.server.embeddings import encode


async def _source(conn, agent_id, **kw):
    cols = {"agent_id": agent_id, "store_id": uuid4(), "text": "Synthetic note.", "speaker": "test-agent"}
    cols.update(kw)
    names = ", ".join(cols)
    params = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
    return await conn.fetchval(f"INSERT INTO sources ({names}) VALUES ({params}) RETURNING id", *cols.values())


async def test_010_recorded(pool):
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version = '010_sources'") == 1


async def test_origin_class_default_and_check(pool, agent):
    async with pool.acquire() as conn:
        sid = await _source(conn, agent["id"])
        assert await conn.fetchval("SELECT origin_class FROM sources WHERE id = $1", sid) == "unspecified"
        with pytest.raises(asyncpg.CheckViolationError):
            await _source(conn, agent["id"], origin_class="rumour")


async def test_store_id_unique(pool, agent):
    store_id = uuid4()
    async with pool.acquire() as conn:
        await _source(conn, agent["id"], store_id=store_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _source(conn, agent["id"], store_id=store_id)


async def test_source_delete_sets_atom_source_null_and_drops_merges(pool, agent):
    text = "The nightly backup runs at 02:00 UTC."
    async with pool.acquire() as conn:
        sid = await _source(conn, agent["id"])
        atom_id = await _insert_atom_sql(conn, agent["id"], "semantic", text, await encode(text), decay_type="none")
        await conn.execute("UPDATE atoms SET source_id = $1, source_quote = $2 WHERE id = $3", sid, text, atom_id)
        await conn.execute("INSERT INTO source_merges (source_id, atom_id, source_quote) VALUES ($1, $2, $3)",
                           sid, atom_id, text)
        await conn.execute("DELETE FROM sources WHERE id = $1", sid)
        row = await conn.fetchrow("SELECT source_id, source_quote FROM atoms WHERE id = $1", atom_id)
        assert row["source_id"] is None and row["source_quote"] == text
        assert await conn.fetchval("SELECT count(*) FROM source_merges WHERE atom_id = $1", atom_id) == 0


async def test_agent_delete_cascades_sources(pool, agent):
    async with pool.acquire() as conn:
        sid = await _source(conn, agent["id"])
        await conn.execute("DELETE FROM agents WHERE id = $1", agent["id"])
        assert await conn.fetchval("SELECT count(*) FROM sources WHERE id = $1", sid) == 0


async def test_store_failures_text_nullable(pool, agent):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO store_failures (id, agent_id, original_text, error) VALUES ($1, $2, NULL, 'x')",
                           uuid4(), agent["id"])
