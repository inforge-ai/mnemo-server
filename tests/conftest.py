"""
Shared fixtures for Mnemo tests.

Strategy:
- Session-scoped asyncpg pool — created once, reused across all tests.
- autouse clean_db — truncates all tables before each test (isolation without
  the overhead of a full DB reconnect per test).
- client fixture — httpx.AsyncClient over ASGITransport. The lifespan is NOT
  triggered (pool is set manually), so no consolidation loop runs during tests.
"""

import os

# Force test database BEFORE mnemo imports so Settings() picks it up at instantiation time.
# This prevents tests from ever touching the production database.
_TEST_DB = "postgresql://mnemo:mnemo@localhost:5432/mnemo_test"
os.environ.setdefault("MNEMO_DATABASE_URL", _TEST_DB)

import asyncpg
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from pgvector.asyncpg import register_vector

from mnemo.server.config import settings

# Hard guard: refuse to run if somehow pointed at a non-test database.
assert "test" in settings.database_url, (
    f"Refusing to run tests against non-test database: {settings.database_url}\n"
    "Set MNEMO_DATABASE_URL to a test database (must contain 'test' in the name)."
)
from mnemo.server.database import set_pool
from mnemo.server.main import app

# Delete in FK-safe order (dependents first); avoids needing TRUNCATE privilege.
# access_log is an immutable audit trail — mnemo user has no DELETE on it,
# and it has no FK dependencies that block cleaning other tables.
_CLEAN = """
DELETE FROM capabilities;
DELETE FROM snapshot_atoms;
DELETE FROM edges;
DELETE FROM views;
DELETE FROM atoms;
DELETE FROM api_keys;
DELETE FROM agents;
DELETE FROM operations;
"""


@pytest_asyncio.fixture(scope="session")
async def pool():
    """Single asyncpg pool shared across the entire test session."""
    p = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=5,
        init=lambda conn: register_vector(conn),
    )
    set_pool(p)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def clean_db(pool):
    """Delete all rows before the test. Used by client/agent fixtures."""
    async with pool.acquire() as conn:
        await conn.execute(_CLEAN)
    yield


@pytest_asyncio.fixture
async def client(pool, clean_db):
    """AsyncClient wired to the FastAPI app via ASGI transport (no real HTTP).
    Depends on clean_db so every test using this fixture starts with an empty DB.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def agent(client):
    """Convenience: register a single agent and return its JSON."""
    resp = await client.post("/v1/agents", json={
        "name": "test-agent",
        "persona": "tester",
        "domain_tags": ["testing"],
    })
    assert resp.status_code == 201
    return resp.json()


@pytest_asyncio.fixture
async def two_agents(client):
    """Convenience: register two agents and return (alice, bob)."""
    r1 = await client.post("/v1/agents", json={"name": "alice", "domain_tags": ["shared"]})
    r2 = await client.post("/v1/agents", json={"name": "bob",   "domain_tags": ["shared"]})
    assert r1.status_code == 201
    assert r2.status_code == 201
    return r1.json(), r2.json()
