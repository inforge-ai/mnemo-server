import uuid

import pytest


async def test_store_status_complete(client, agent):
    """After storing, the status endpoint should return 'complete'."""
    agent_id = agent["id"]
    resp = await client.post(
        f"/v1/agents/{agent_id}/remember",
        json={"text": "The deployment runs on Hetzner cloud infrastructure."},
    )
    assert resp.status_code == 201
    store_id = resp.json()["store_id"]

    # In test mode (sync_store_for_tests=True), store is already complete
    status_resp = await client.get(f"/v1/stores/{store_id}/status")
    assert status_resp.status_code == 200
    data = status_resp.json()
    assert data["store_id"] == store_id
    assert data["status"] == "complete"
    assert data["atoms_created"] >= 1


async def test_store_status_not_found(client):
    """Querying a non-existent store_id should return 404."""
    fake_id = str(uuid.uuid4())
    resp = await client.get(f"/v1/stores/{fake_id}/status")
    assert resp.status_code == 404


async def test_store_status_failed(client, agent, pool):
    """A failed store should report status='failed'."""
    agent_id = agent["id"]
    store_id = uuid.uuid4()

    async with pool.acquire() as conn:
        op_row = await conn.fetchrow(
            "SELECT operator_id FROM agents WHERE id = $1", agent_id,
        )
        await conn.execute(
            """
            INSERT INTO store_jobs (store_id, agent_id, operator_id, status, error)
            VALUES ($1, $2, $3, 'failed', 'Test error message')
            """,
            store_id, agent_id, op_row["operator_id"],
        )

    resp = await client.get(f"/v1/stores/{store_id}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert "error" in data


# Cross-operator isolation test skipped: requires MNEMO_AUTH_ENABLED=true.
# When auth is disabled, operator["id"] is None and the operator filter is
# bypassed. The query correctly scopes by operator_id when auth is active.
