import json
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import AuthContext, require_admin, require_agent, require_agent_match, require_operator, resolve_auth
from ..database import get_conn, get_pool
from ..services.address_service import create_address, resolve_address as resolve_address_fn, resolve_agent_identifier
from ..models import AgentCreate, AgentCreateResponse, AgentResponse, AgentStats
from ..services.agent_service import depart_agent as do_depart, reinstate_agent as do_reinstate
from ..services.atom_service import get_agent_stats
from ..services.auth_service import create_agent_key, get_or_create_local_operator

router = APIRouter(tags=["agents"])


@router.post("/agents", response_model=AgentCreateResponse, status_code=201)
async def register_agent(body: AgentCreate, auth: AuthContext = Depends(require_operator)):
    try:
        async with get_conn() as conn:
            if auth.operator_id is not None:
                operator_id = auth.operator_id
            else:
                # Auth disabled or admin — use the local operator
                operator_id = await get_or_create_local_operator(conn)

            row = await conn.fetchrow(
                """
                INSERT INTO agents (operator_id, name, persona, domain_tags, metadata)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, operator_id, name, persona, domain_tags, metadata, created_at, status
                """,
                operator_id,
                body.name,
                body.persona,
                body.domain_tags,
                json.dumps(body.metadata),
            )

            # Generate agent key (returned once, stored as hash)
            agent_key = await create_agent_key(conn, row["id"])

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
    result = _agent_row(row, address=address)
    result["agent_key"] = agent_key
    return result


@router.get("/agents", response_model=list[AgentResponse])
async def list_agents(
    name: str | None = Query(None, description="Filter by exact agent name"),
    auth: AuthContext = Depends(require_operator),
):
    """List agents. Returns only the operator's agents.
    Optional name filter for exact match lookup."""
    async with get_conn() as conn:
        if auth.operator_id is not None:
            operator_id = auth.operator_id
            if name is not None:
                rows = await conn.fetch(
                    """
                    SELECT a.id, a.operator_id, a.name, a.persona, a.domain_tags, a.metadata, a.created_at, a.status,
                           aa.address
                    FROM agents a
                    LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
                    WHERE a.operator_id = $1 AND a.name = $2 AND a.status = 'active'
                    ORDER BY a.created_at ASC
                    """,
                    operator_id,
                    name,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT a.id, a.operator_id, a.name, a.persona, a.domain_tags, a.metadata, a.created_at, a.status,
                           aa.address
                    FROM agents a
                    LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
                    WHERE a.operator_id = $1 AND a.status = 'active'
                    ORDER BY a.created_at ASC
                    """,
                    operator_id,
                )
        else:
            # Auth disabled — list all or filter by name
            if name is not None:
                rows = await conn.fetch(
                    """
                    SELECT a.id, a.operator_id, a.name, a.persona, a.domain_tags, a.metadata, a.created_at, a.status,
                           aa.address
                    FROM agents a
                    LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
                    WHERE a.name = $1 AND a.status = 'active'
                    ORDER BY a.created_at ASC
                    """,
                    name,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT a.id, a.operator_id, a.name, a.persona, a.domain_tags, a.metadata, a.created_at, a.status,
                           aa.address
                    FROM agents a
                    LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
                    WHERE a.status = 'active'
                    ORDER BY a.created_at ASC
                    """,
                )
    return [_agent_row(r) for r in rows]


@router.get("/agents/resolve/{address:path}")
async def resolve_agent_address(address: str, auth: AuthContext = Depends(resolve_auth)):
    """Resolve an agent address to agent info. Any authenticated user can resolve."""
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
            SELECT a.id, a.operator_id, a.name, a.persona, a.domain_tags, a.metadata, a.created_at, a.status,
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
async def agent_stats(agent_id: str, auth: AuthContext = Depends(require_agent)):
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    require_agent_match(agent_uuid, auth)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        stats = await get_agent_stats(conn, agent_uuid)
        addr_row = await conn.fetchrow(
            "SELECT address FROM agent_addresses WHERE agent_id = $1", agent_uuid
        )
        stats["address"] = addr_row["address"] if addr_row else None
    return stats


@router.post("/agents/{agent_id}/rotate-key")
async def rotate_agent_key(agent_id: str, auth: AuthContext = Depends(require_operator)):
    """
    Rotate an agent's key (operator or admin).
    Overwrites the existing key hash and returns the new plaintext key once.
    """
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)

    async with get_conn() as conn:
        # Verify agent exists and is active
        row = await conn.fetchrow(
            "SELECT id, name, operator_id FROM agents WHERE id = $1",
            agent_uuid,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Agent not found")
        if row["operator_id"] != auth.operator_id and auth.role != "admin":
            raise HTTPException(status_code=403, detail="Agent not owned by this operator")

        new_key = await create_agent_key(conn, agent_uuid)

        addr_row = await conn.fetchrow(
            "SELECT address FROM agent_addresses WHERE agent_id = $1", agent_uuid
        )

    return {
        "agent_id": str(agent_uuid),
        "name": row["name"],
        "address": addr_row["address"] if addr_row else None,
        "agent_key": new_key,
        "message": "Save this key — it will not be shown again. The previous key is now invalid.",
    }


@router.post("/agents/{agent_id}/depart")
async def depart_agent(agent_id: str, auth: AuthContext = Depends(require_admin)):
    """
    Agent departure (admin only):
    1. Cascade-revoke all capabilities this agent granted.
    2. Mark agent inactive with departure + expiry timestamps.
    3. Return summary.
    """
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    async with get_conn() as conn:
        try:
            result = await do_depart(conn, agent_uuid)
        except ValueError as e:
            detail = str(e)
            code = 404 if "not found" in detail else 409
            raise HTTPException(status_code=code, detail=detail)
    return result


@router.post("/agents/{agent_id}/reactivate")
async def reactivate_agent(agent_id: str, auth: AuthContext = Depends(require_admin)):
    """
    Reactivate a departed agent (admin only):
    1. Clear departure and expiry timestamps.
    2. Mark agent active.
    3. Log the reactivation.

    Note: capabilities revoked during departure are NOT restored.
    """
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    async with get_conn() as conn:
        try:
            result = await do_reinstate(conn, agent_uuid)
        except ValueError as e:
            detail = str(e)
            code = 404 if "not found" in detail else 409
            raise HTTPException(status_code=code, detail=detail)
    return {**result, "message": "Agent reactivated. Previously revoked capabilities must be re-granted."}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _agent_row(row, address: str | None = None) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "persona": row["persona"],
        "domain_tags": list(row["domain_tags"]) if row["domain_tags"] else [],
        "metadata": json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {}),
        "created_at": row["created_at"],
        "status": row["status"],
        "address": address if address is not None else row.get("address"),
    }


async def _require_active_agent(conn, agent_id: UUID):
    row = await conn.fetchrow(
        "SELECT status FROM agents WHERE id = $1",
        agent_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    if row["status"] != "active":
        raise HTTPException(status_code=410, detail="Agent has departed")
