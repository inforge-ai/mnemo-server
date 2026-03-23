"""Shared agent lifecycle operations (depart / reinstate)."""

import json
from uuid import UUID


async def depart_agent(conn, agent_id: UUID) -> dict:
    """
    Depart an agent:
    1. Cascade-revoke capabilities
    2. Set status='departed', departed_at, data_expires_at
    3. Audit log
    Returns dict with revoked count, departed_at, data_expires_at.
    Raises ValueError if agent not found or already departed.
    """
    row = await conn.fetchrow("SELECT id, status FROM agents WHERE id = $1", agent_id)
    if not row:
        raise ValueError("Agent not found")
    if row["status"] != "active":
        raise ValueError("Agent already departed")

    revoked_count = await conn.fetchval(
        "SELECT revoke_agent_capabilities($1)", agent_id
    )

    updated = await conn.fetchrow(
        """
        UPDATE agents
        SET status = 'departed',
            departed_at = now(),
            data_expires_at = now() + interval '30 days'
        WHERE id = $1
        RETURNING departed_at, data_expires_at
        """,
        agent_id,
    )

    await conn.execute(
        """
        INSERT INTO access_log (agent_id, action, metadata)
        VALUES ($1, 'departure', $2)
        """,
        agent_id,
        json.dumps({"capabilities_revoked": revoked_count}),
    )

    return {
        "capabilities_revoked": revoked_count,
        "departed_at": updated["departed_at"],
        "data_expires_at": updated["data_expires_at"],
    }


async def reinstate_agent(conn, agent_id: UUID) -> dict:
    """
    Reinstate a departed agent.
    Returns dict with agent info.
    Raises ValueError if not found, already active, or operator not active.
    """
    row = await conn.fetchrow(
        """
        SELECT a.id, a.status, a.name, o.status AS operator_status, o.username AS operator_username
        FROM agents a JOIN operators o ON o.id = a.operator_id
        WHERE a.id = $1
        """,
        agent_id,
    )
    if not row:
        raise ValueError("Agent not found")
    if row["status"] == "active":
        raise ValueError("Agent is already active")
    if row["operator_status"] != "active":
        raise ValueError(
            f"Cannot reinstate: operator '{row['operator_username']}' is {row['operator_status']}"
        )

    updated = await conn.fetchrow(
        """
        UPDATE agents
        SET status = 'active',
            departed_at = NULL,
            data_expires_at = NULL
        WHERE id = $1
        RETURNING name, created_at
        """,
        agent_id,
    )

    await conn.execute(
        """
        INSERT INTO access_log (agent_id, action, metadata)
        VALUES ($1, 'reactivation', '{}')
        """,
        agent_id,
    )

    return {"id": str(agent_id), "name": updated["name"], "status": "active"}
