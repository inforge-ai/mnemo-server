import json
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import get_current_operator, verify_agent_ownership
from ..database import get_conn, get_pool
from ..services.address_service import create_address, resolve_address as resolve_address_fn, resolve_agent_identifier
from ..models import AgentCreate, AgentResponse, AgentStats
from ..services.atom_service import get_agent_stats
from ..services.auth_service import get_or_create_local_operator

router = APIRouter(tags=["agents"])


@router.post("/agents", response_model=AgentResponse, status_code=201)
async def register_agent(body: AgentCreate, operator=Depends(get_current_operator)):
    try:
        async with get_conn() as conn:
            if operator["id"] is not None:
                operator_id = UUID(operator["id"])
            else:
                # Auth disabled — use the local operator
                operator_id = await get_or_create_local_operator(conn)

            row = await conn.fetchrow(
                """
                INSERT INTO agents (operator_id, name, persona, domain_tags, metadata)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, operator_id, name, persona, domain_tags, metadata, created_at, is_active
                """,
                operator_id,
                body.name,
                body.persona,
                body.domain_tags,
                json.dumps(body.metadata),
            )

            # Populate agent address
            op_row = await conn.fetchrow(
                "SELECT username, org FROM operators WHERE id = $1", operator_id
            )
            address = None
            if op_row:
                address = await create_address(conn, row["id"], body.name, op_row["username"], op_row["org"])

                # Auto-seed symmetric trust rows for all agents in the same org
                if op_row["org"]:
                    await conn.execute(
                        """
                        INSERT INTO agent_trust (agent_uuid, trusted_sender_uuid)
                        SELECT $1, a.id FROM agents a
                        JOIN operators o ON o.id = a.operator_id
                        WHERE o.org = $2 AND a.id != $1
                        UNION ALL
                        SELECT a.id, $1 FROM agents a
                        JOIN operators o ON o.id = a.operator_id
                        WHERE o.org = $2 AND a.id != $1
                        ON CONFLICT DO NOTHING
                        """,
                        row["id"],
                        op_row["org"],
                    )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail=f"Agent name '{body.name}' already exists")
    return _agent_row(row, address=address)


@router.get("/agents", response_model=list[AgentResponse])
async def list_agents(
    name: str | None = Query(None, description="Filter by exact agent name"),
    operator=Depends(get_current_operator),
):
    """List agents. When auth is enabled, returns only the operator's agents.
    Optional name filter for exact match lookup."""
    async with get_conn() as conn:
        if operator["id"] is not None:
            operator_id = UUID(operator["id"])
            if name is not None:
                rows = await conn.fetch(
                    """
                    SELECT a.id, a.operator_id, a.name, a.persona, a.domain_tags, a.metadata, a.created_at, a.is_active,
                           aa.address
                    FROM agents a
                    LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
                    WHERE a.operator_id = $1 AND a.name = $2 AND a.is_active = true
                    ORDER BY a.created_at ASC
                    """,
                    operator_id,
                    name,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT a.id, a.operator_id, a.name, a.persona, a.domain_tags, a.metadata, a.created_at, a.is_active,
                           aa.address
                    FROM agents a
                    LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
                    WHERE a.operator_id = $1 AND a.is_active = true
                    ORDER BY a.created_at ASC
                    """,
                    operator_id,
                )
        else:
            # Auth disabled — list all or filter by name
            if name is not None:
                rows = await conn.fetch(
                    """
                    SELECT a.id, a.operator_id, a.name, a.persona, a.domain_tags, a.metadata, a.created_at, a.is_active,
                           aa.address
                    FROM agents a
                    LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
                    WHERE a.name = $1 AND a.is_active = true
                    ORDER BY a.created_at ASC
                    """,
                    name,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT a.id, a.operator_id, a.name, a.persona, a.domain_tags, a.metadata, a.created_at, a.is_active,
                           aa.address
                    FROM agents a
                    LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
                    WHERE a.is_active = true
                    ORDER BY a.created_at ASC
                    """,
                )
    return [_agent_row(r) for r in rows]


@router.get("/agents/resolve/{address:path}")
async def resolve_agent_address(address: str, operator=Depends(get_current_operator)):
    """Resolve an agent address to agent info. Any authenticated operator can resolve."""
    pool = await get_pool()
    agent_id = await resolve_address_fn(pool, address)
    if not agent_id:
        raise HTTPException(status_code=404, detail=f"Agent not found: {address}")
    async with get_conn() as conn:
        row = await conn.fetchrow("""
            SELECT a.id, a.name, aa.address, o.name AS operator_name
            FROM agents a
            LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
            JOIN operators o ON o.id = a.operator_id
            WHERE a.id = $1
        """, agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {
        "agent_id": str(row["id"]),
        "name": row["name"],
        "address": row["address"],
        "operator": row["operator_name"],
    }


@router.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str):
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT a.id, a.operator_id, a.name, a.persona, a.domain_tags, a.metadata, a.created_at, a.is_active,
                   aa.address
            FROM agents a
            LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
            WHERE a.id = $1
            """,
            agent_uuid,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _agent_row(row)


@router.get("/agents/{agent_id}/stats", response_model=AgentStats)
async def agent_stats(agent_id: str, operator=Depends(get_current_operator)):
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        stats = await get_agent_stats(conn, agent_uuid)
        addr_row = await conn.fetchrow(
            "SELECT address FROM agent_addresses WHERE agent_id = $1", agent_uuid
        )
        stats["address"] = addr_row["address"] if addr_row else None
    return stats


@router.post("/agents/{agent_id}/depart")
async def depart_agent(agent_id: str, operator=Depends(get_current_operator)):
    """
    Agent departure:
    1. Cascade-revoke all capabilities this agent granted.
    2. Mark agent inactive with departure + expiry timestamps.
    3. Return summary.
    """
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        row = await conn.fetchrow(
            "SELECT id, is_active FROM agents WHERE id = $1",
            agent_uuid,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Agent not found")
        if not row["is_active"]:
            raise HTTPException(status_code=409, detail="Agent already departed")

        # Cascade-revoke capabilities
        revoked_count = await conn.fetchval(
            "SELECT revoke_agent_capabilities($1)",
            agent_uuid,
        )

        # Mark departed
        row = await conn.fetchrow(
            """
            UPDATE agents
            SET is_active       = false,
                departed_at     = now(),
                data_expires_at = now() + interval '30 days'
            WHERE id = $1
            RETURNING departed_at, data_expires_at
            """,
            agent_uuid,
        )

        # Audit log
        await conn.execute(
            """
            INSERT INTO access_log (agent_id, action, metadata)
            VALUES ($1, 'departure', $2)
            """,
            agent_uuid,
            json.dumps({"capabilities_revoked": revoked_count}),
        )

    return {
        "capabilities_revoked": revoked_count,
        "departed_at": row["departed_at"],
        "data_expires_at": row["data_expires_at"],
    }


@router.post("/agents/{agent_id}/reactivate")
async def reactivate_agent(agent_id: str, operator=Depends(get_current_operator)):
    """
    Reactivate a departed agent:
    1. Clear departure and expiry timestamps.
    2. Mark agent active.
    3. Log the reactivation.

    Note: capabilities revoked during departure are NOT restored.
    """
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        row = await conn.fetchrow(
            "SELECT id, is_active, departed_at FROM agents WHERE id = $1",
            agent_uuid,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Agent not found")
        if row["is_active"]:
            raise HTTPException(status_code=409, detail="Agent is already active")

        updated = await conn.fetchrow(
            """
            UPDATE agents
            SET is_active       = true,
                departed_at     = NULL,
                data_expires_at = NULL
            WHERE id = $1
            RETURNING name, created_at
            """,
            agent_uuid,
        )

        # Audit log
        await conn.execute(
            """
            INSERT INTO access_log (agent_id, action, metadata)
            VALUES ($1, 'reactivation', '{}')
            """,
            agent_uuid,
        )

    return {
        "id": str(agent_uuid),
        "name": updated["name"],
        "is_active": True,
        "message": "Agent reactivated. Previously revoked capabilities must be re-granted.",
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _agent_row(row, address: str | None = None) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "persona": row["persona"],
        "domain_tags": list(row["domain_tags"]) if row["domain_tags"] else [],
        "metadata": json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {}),
        "created_at": row["created_at"],
        "is_active": row["is_active"],
        "address": address if address is not None else row.get("address"),
    }


async def _require_active_agent(conn, agent_id: UUID):
    row = await conn.fetchrow(
        "SELECT is_active FROM agents WHERE id = $1",
        agent_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not row["is_active"]:
        raise HTTPException(status_code=410, detail="Agent has departed")
