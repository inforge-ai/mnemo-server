"""store_from_text reports which atom IDs are newly inserted (vs merged)."""
import pytest


@pytest.mark.asyncio
async def test_store_from_text_returns_new_atom_ids_for_fresh_atoms(pool, agent_with_address):
    from mnemo.server.services.atom_service import store_from_text
    agent_id = agent_with_address["id"]

    async with pool.acquire() as conn:
        async with conn.transaction():
            r1 = await store_from_text(conn, agent_id, "Sky is blue.", ["t"])
        assert "new_atom_ids" in r1
        assert len(r1["new_atom_ids"]) == r1["atoms_created"]
        assert set(r1["new_atom_ids"]) == {a["id"] for a in r1["atoms"]}


@pytest.mark.asyncio
async def test_store_from_text_excludes_merged_duplicates_from_new_ids(pool, agent_with_address):
    from mnemo.server.services.atom_service import store_from_text
    agent_id = agent_with_address["id"]

    async with pool.acquire() as conn:
        async with conn.transaction():
            r1 = await store_from_text(conn, agent_id, "Pluto is a dwarf planet.", ["t"])
        async with conn.transaction():
            r2 = await store_from_text(conn, agent_id, "Pluto is a dwarf planet.", ["t"])
        assert r2["duplicates_merged"] >= 1
        assert r2["new_atom_ids"] == []
