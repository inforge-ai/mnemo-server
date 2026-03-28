"""
Shared fixtures for Mnemo tests.

Strategy:
- Session-scoped asyncpg pool — created once, reused across all tests.
- autouse clean_db — truncates all tables before each test (isolation without
  the overhead of a full DB reconnect per test).
- client fixture — httpx.AsyncClient over ASGITransport. The lifespan is NOT
  triggered (pool is set manually), so no consolidation loop runs during tests.
- sync_store_for_tests=True — /remember awaits store_background inline so the
  POST itself blocks until storage is complete.
- RBAC auth fixtures — admin_key, operator_with_key, agent_with_key provide
  proper credentials for each role.
"""

import os

# Override database_url with test_database_url so the test suite uses the test DB.
# MNEMO_TEST_DATABASE_URL must be set in .env or environment.
from dotenv import load_dotenv
load_dotenv()

_test_url = os.environ.get("MNEMO_TEST_DATABASE_URL", "")
if not _test_url:
    raise RuntimeError(
        "MNEMO_TEST_DATABASE_URL is not set. "
        "Add it to your .env file, e.g.: MNEMO_TEST_DATABASE_URL=postgresql://mnemo:pw@localhost:5432/mnemo_test"
    )
os.environ["MNEMO_DATABASE_URL"] = _test_url
os.environ.setdefault("MNEMO_SYNC_STORE_FOR_TESTS", "true")

# Set a known admin key for tests
TEST_ADMIN_KEY = "test-admin-key-for-rbac-tests"
os.environ["MNEMO_ADMIN_KEY"] = TEST_ADMIN_KEY

import asyncpg
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from pgvector.asyncpg import register_vector

from mnemo.server.config import settings

# Hard guard: refuse to run if somehow pointed at a non-test database.
assert "test" in settings.database_url, (
    f"Refusing to run tests against non-test database: {settings.database_url}\n"
    "Set MNEMO_TEST_DATABASE_URL to a database with 'test' in the name."
)

# Ensure admin key is set for tests
settings.admin_key = TEST_ADMIN_KEY

from mnemo.server.database import set_pool
from mnemo.server.main import app

# Delete in FK-safe order (dependents first); avoids needing TRUNCATE privilege.
# access_log is an immutable audit trail — mnemo user has no DELETE on it,
# and it has no FK dependencies that block cleaning other tables.
_CLEAN = """
DELETE FROM agent_trust;
DELETE FROM capabilities;
DELETE FROM snapshot_atoms;
DELETE FROM edges;
DELETE FROM views;
DELETE FROM store_failures;
DELETE FROM store_jobs;
DELETE FROM decomposer_usage;
DELETE FROM atoms;
DELETE FROM api_keys;
DELETE FROM agent_addresses;
DELETE FROM agents;
DELETE FROM operations;
DELETE FROM operators;
DELETE FROM platform_config;
"""


def pytest_configure(config):
    """Pre-load the embedding model synchronously before any tests run.

    encode() uses a ThreadPoolExecutor — calling it async before the first test
    doesn't block test startup. Calling the sync warmup() here guarantees the
    model is fully loaded before any background store task runs.
    """
    from mnemo.server.embeddings import warmup
    warmup()


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


# ── RBAC Auth Fixtures ─────────────────────────────────────────────────────


def admin_headers() -> dict:
    """Return headers for admin-level access."""
    return {"X-Admin-Key": TEST_ADMIN_KEY}


@pytest_asyncio.fixture
async def operator_with_key(client):
    """Create an operator via admin API, return (operator_data, operator_key, auth_headers).

    operator_data: the full response JSON from POST /v1/admin/operators
    operator_key: the plaintext API key (mnemo_ prefixed)
    headers: dict with X-Operator-Key header ready for use
    """
    resp = await client.post("/v1/admin/operators", headers=admin_headers(), json={
        "username": "testop",
        "org": "testorg",
        "display_name": "Test Operator",
        "email": "testop@test.com",
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()

    # Enable full sharing for test operator so sharing tests work
    resp = await client.patch(
        f"/v1/admin/operators/{data['uuid']}/sharing-scope",
        headers=admin_headers(),
        json={"sharing_scope": "full"},
    )
    assert resp.status_code == 200, resp.text

    op_key = data["api_key"]
    headers = {"X-Operator-Key": op_key}
    return data, op_key, headers


@pytest_asyncio.fixture
async def agent_with_key(client, operator_with_key):
    """Register an agent via operator key, return (agent_data, agent_key, auth_headers).

    agent_data: the full response JSON including id, name, address, agent_key
    agent_key: the plaintext agent key (mnemo_ag_ prefixed)
    headers: dict with X-Agent-Key header ready for use
    """
    _, _, op_headers = operator_with_key
    resp = await client.post("/v1/agents", json={
        "name": "test-agent",
        "persona": "tester",
        "domain_tags": ["testing"],
    }, headers=op_headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    ag_key = data["agent_key"]
    headers = {"X-Agent-Key": ag_key}
    return data, ag_key, headers


@pytest_asyncio.fixture
async def agent(client, operator_with_key):
    """Convenience: register a single agent and return its JSON (backward compat)."""
    _, _, op_headers = operator_with_key
    resp = await client.post("/v1/agents", json={
        "name": "test-agent",
        "persona": "tester",
        "domain_tags": ["testing"],
    }, headers=op_headers)
    assert resp.status_code == 201
    return resp.json()


@pytest_asyncio.fixture
async def two_agents(client, operator_with_key):
    """Register two agents and return (alice, bob) dicts."""
    _, _, op_headers = operator_with_key
    r1 = await client.post("/v1/agents", json={"name": "alice", "domain_tags": ["shared"]}, headers=op_headers)
    r2 = await client.post("/v1/agents", json={"name": "bob", "domain_tags": ["shared"]}, headers=op_headers)
    assert r1.status_code == 201
    assert r2.status_code == 201
    return r1.json(), r2.json()


@pytest_asyncio.fixture
async def operator_with_username(pool, clean_db):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO operators (name, username, org, sharing_scope)
            VALUES ('Test Operator', 'testuser', 'testorg', 'full')
            RETURNING id, name, username, org
        """)
    return dict(row)


@pytest_asyncio.fixture
async def agent_with_address(client, pool, operator_with_username):
    op = operator_with_username
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO agents (operator_id, name, domain_tags)
            VALUES ($1, 'test-agent', '{"testing"}')
            RETURNING id, name, operator_id, persona, domain_tags, metadata, created_at, status
        """, op["id"])
        from mnemo.server.services.address_service import create_address
        from mnemo.server.services.auth_service import create_agent_key
        address = await create_address(conn, row["id"], row["name"], op["username"], op["org"])
        agent_key = await create_agent_key(conn, row["id"])
    agent = dict(row)
    agent["address"] = address
    agent["operator"] = op
    agent["agent_key"] = agent_key
    return agent


async def remember(
    client,
    agent_id: str,
    text: str,
    domain_tags: list[str] | None = None,
    headers: dict | None = None,
):
    """Call /remember and wait for storage to complete.

    With MNEMO_SYNC_STORE_FOR_TESTS=true, the route awaits store_background
    inline before returning, so the POST itself blocks until all atoms are
    stored. No background task coordination needed.

    Pass headers={"X-Agent-Key": key} for auth. If headers is None and
    auth is disabled, no auth header is sent.
    """
    body = {"text": text}
    if domain_tags:
        body["domain_tags"] = domain_tags
    resp = await client.post(
        f"/v1/agents/{agent_id}/remember",
        json=body,
        headers=headers or {},
    )
    assert resp.status_code == 201, f"remember failed: {resp.status_code} {resp.text}"
    assert resp.json()["status"] == "queued"
    return resp
