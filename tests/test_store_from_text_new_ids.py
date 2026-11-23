"""persist_atoms reports which atom IDs are newly inserted (vs merged)."""
from tests.helpers import store_text


async def test_returns_new_atom_ids_for_fresh_atoms(pool, agent_with_address):
    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        r1 = await store_text(conn, agent_id, "Sky is blue.", ["t"])
    assert "new_atom_ids" in r1
    assert len(r1["new_atom_ids"]) == r1["atoms_created"]
    assert set(r1["new_atom_ids"]) == {a["id"] for a in r1["atoms"]}


async def test_excludes_merged_duplicates_from_new_ids(pool, agent_with_address):
    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        await store_text(conn, agent_id, "Pluto is a dwarf planet.", ["t"])
        r2 = await store_text(conn, agent_id, "Pluto is a dwarf planet.", ["t"])
    assert r2["duplicates_merged"] >= 1
    assert r2["new_atom_ids"] == []
    assert all(a["merged_into_existing"] for a in r2["atoms"])
