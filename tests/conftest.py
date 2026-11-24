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

# Tests never read the production .env. Settings come from .env.test (local,
# git-ignored; see .env.test.example) or from the environment (CI). The
# values in .env.test override the shell so a production MNEMO_DATABASE_URL
# exported in the shell cannot leak in.
os.environ["MNEMO_ENV_FILE"] = ".env.test"
from dotenv import load_dotenv
load_dotenv(".env.test", override=True)

if "test" not in os.environ.get("MNEMO_DATABASE_URL", ""):
    raise RuntimeError(
        "MNEMO_DATABASE_URL must point at a database with 'test' in its name. "
        "Copy .env.test.example to .env.test (scripts/test/build_test_db.sh builds the DB)."
    )
os.environ.setdefault("MNEMO_SYNC_STORE_FOR_TESTS", "true")
# Post-store lifecycle detection calls an LLM; tests that need it enable it.
os.environ.setdefault("MNEMO_LIFECYCLE_DETECTION_ENABLED", "false")
# Unit tests: no gated embedding model, no live LLM. `-m eval` switches both
# to the real thing in pytest_configure.
os.environ.setdefault("MNEMO_EMBEDDING_BACKEND", "fake")
os.environ.setdefault("MNEMO_DECOMPOSER_PROVIDER", "fake")

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
    "Refusing to run tests against a non-test database. "
    "Set MNEMO_DATABASE_URL in .env.test to a database with 'test' in the name."
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
DELETE FROM lifecycle_dlq;
DELETE FROM conflicts;
DELETE FROM relation_decisions;
DELETE FROM edges;
DELETE FROM views;
DELETE FROM store_failures;
DELETE FROM store_jobs;
DELETE FROM decomposer_usage;
DELETE FROM source_merges;
DELETE FROM atoms;
DELETE FROM sources;
DELETE FROM api_keys;
DELETE FROM agent_addresses;
DELETE FROM agents;
DELETE FROM operations;
DELETE FROM operators;
DELETE FROM platform_config;
"""


_LIVE_KEYS = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")


def _is_eval_run(config) -> bool:
    expr = (config.option.markexpr or "").replace(" ", "")
    return "eval" in expr and "noteval" not in expr


def pytest_configure(config):
    """Non-eval runs: strip live-model credentials so nothing can call out.
    Eval runs (`-m eval`): real embedder + real decomposer, keys from the
    shell environment (never from the production .env).
    """
    config._mnemo_eval = _is_eval_run(config)
    if config._mnemo_eval:
        settings.embedding_backend = "sentence_transformers"
        settings.decomposer_provider = "anthropic"
        # Production runs lifecycle detection (supersedes / tension edges).
        settings.lifecycle_detection_enabled = True
        from mnemo.server.embeddings import warmup
        warmup()
    else:
        for key in _LIVE_KEYS:
            os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def _no_live_llm(request, monkeypatch):
    """Outside eval runs, constructing a real Anthropic client fails the test.

    Tests that exercise the LLM paths patch `_get_client` (or the client
    class) with a mock, which never reaches this constructor.
    """
    if request.config._mnemo_eval:
        yield
        return
    import anthropic
    from mnemo.server.decomposers import anthropic as anthropic_decomposer, fake as fake_decomposer
    from mnemo.server.services import lifecycle_service

    attempts: list[str] = []
    request.node._live_llm_attempts = attempts

    def _refuse(self, *args, **kwargs):
        attempts.append(request.node.nodeid)
        raise RuntimeError("live Anthropic client constructed in a non-eval test")

    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", _refuse)
    anthropic_decomposer._get_client.cache_clear()
    if hasattr(lifecycle_service._get_client, "cache_clear"):
        lifecycle_service._get_client.cache_clear()
    fake_decomposer.reset()
    yield
    fake_decomposer.reset()
    if attempts:
        pytest.fail("test tried to construct a live Anthropic client; mock it or mark it eval")


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

    v2 Phase 1: wait=true is the default, so the POST returns once storage
    is complete and lists the stored atoms.

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
    assert resp.json()["status"] == "complete"
    return resp
