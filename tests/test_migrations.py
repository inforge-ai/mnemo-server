"""
Migration runner (v2 Phase 0).

Each DB test runs inside an outer transaction that is rolled back, so
schema_migrations rows and temp tables never persist. apply_pending's own
per-migration transactions nest as savepoints.
"""

import re
from pathlib import Path

import pytest

from mnemo.server.services import migration_service as ms

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
async def conn(pool):
    async with pool.acquire() as c:
        tr = c.transaction()
        await tr.start()
        try:
            yield c
        finally:
            await tr.rollback()


@pytest.fixture
def mig_dir(tmp_path):
    (tmp_path / "901_alpha.sql").write_text("CREATE TEMP TABLE phase0_alpha (x int);")
    (tmp_path / "902_beta.sql").write_text("CREATE TEMP TABLE phase0_beta (x int);")
    (tmp_path / "903_gamma.sql").write_text("CREATE TEMP TABLE phase0_gamma (x int);")
    (tmp_path / "README.txt").write_text("not a migration")
    return tmp_path


async def _exists(conn, name):
    return await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"pg_temp.{name}")


async def test_list_pending_in_order_ignoring_stray_rows(conn, mig_dir):
    await conn.execute("INSERT INTO schema_migrations (version) VALUES ('901_alpha'), ('002') ON CONFLICT DO NOTHING")
    assert await ms.list_pending(conn, mig_dir) == ["902_beta", "903_gamma"]


async def test_apply_pending_records_and_is_idempotent(conn, mig_dir):
    assert await ms.apply_pending(conn, mig_dir, dry_run=True) == ["901_alpha", "902_beta", "903_gamma"]
    assert not await _exists(conn, "phase0_alpha")  # dry run applies nothing

    assert await ms.apply_pending(conn, mig_dir) == ["901_alpha", "902_beta", "903_gamma"]
    for t in ("phase0_alpha", "phase0_beta", "phase0_gamma"):
        assert await _exists(conn, t)
    assert await ms.list_pending(conn, mig_dir) == []
    assert await ms.apply_pending(conn, mig_dir) == []


async def test_apply_pending_to_stops_inclusive(conn, mig_dir):
    assert await ms.apply_pending(conn, mig_dir, to="902_beta") == ["901_alpha", "902_beta"]
    assert await ms.list_pending(conn, mig_dir) == ["903_gamma"]
    with pytest.raises(ValueError):
        await ms.apply_pending(conn, mig_dir, to="999_nope")


async def test_failed_migration_rolls_back_only_itself(conn, mig_dir):
    (mig_dir / "902_beta.sql").write_text(
        "CREATE TEMP TABLE phase0_beta (x int); SELECT 1/0;"
    )
    with pytest.raises(Exception):
        await ms.apply_pending(conn, mig_dir)
    assert await _exists(conn, "phase0_alpha")
    assert not await _exists(conn, "phase0_beta")
    assert await ms.list_pending(conn, mig_dir) == ["902_beta", "903_gamma"]


async def test_run_migrations_fails_startup_when_pending(pool, mig_dir, monkeypatch):
    from mnemo.server.config import settings
    monkeypatch.setattr(ms, "MIGRATIONS_DIR", mig_dir)

    monkeypatch.setattr(settings, "require_migrations", True)
    with pytest.raises(ms.PendingMigrationsError) as exc:
        await ms.run_migrations(pool)
    assert exc.value.pending == ["901_alpha", "902_beta", "903_gamma"]

    monkeypatch.setattr(settings, "require_migrations", False)
    assert await ms.run_migrations(pool) == ["901_alpha", "902_beta", "903_gamma"]


async def test_test_database_is_fully_migrated(pool):
    """The test DB must be built with schema.sql + `mnemo migrate`."""
    async with pool.acquire() as c:
        assert await ms.list_pending(c) == []


def test_schema_sql_records_baseline_versions():
    """schema.sql contains 001–005 and records exactly those; 006+ come from
    `mnemo migrate`."""
    schema = (REPO / "schema.sql").read_text()
    block = re.search(r"INSERT INTO schema_migrations \(version\) VALUES(.*?);", schema, re.S)
    assert block
    recorded = re.findall(r"'([^']+)'", block.group(1))
    stems = [f.stem for f in ms.migration_files()]
    assert recorded == [s for s in stems if s[:3] <= "005"]
    assert all(v in stems for v in recorded)


async def test_no_self_loop_edges_allowed(pool):
    """Migration 007 adds edges_no_self_loop."""
    async with pool.acquire() as c:
        defn = await c.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'edges_no_self_loop'"
        )
    assert defn is not None and "source_id <> target_id" in defn
