"""
Admin agent endpoints — protected by X-Admin-Token header.

Endpoints:
  GET    /v1/admin/agents                        — list all agents
  POST   /v1/admin/agents/{agent_id}/depart      — admin force-depart
  POST   /v1/admin/agents/{agent_id}/reinstate   — admin reinstate
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import get_conn, get_pool
from ..services.address_service import resolve_agent_identifier
from ..services.agent_service import depart_agent as do_depart, reinstate_agent as do_reinstate
from .admin import _require_admin

router = APIRouter(tags=["admin"], prefix="/admin/agents")


@router.get("", dependencies=[Depends(_require_admin)])
async def list_agents(
    operator: str | None = Query(None, description="Filter by operator UUID"),
    status: str | None = Query(None, description="Filter by status (active|departed)"),
):
    """List all agents with address and operator info."""
    async with get_conn() as conn:
        # Build query with optional filters
        conditions = []
        params = []
        idx = 1

        if operator is not None:
            try:
                op_uuid = UUID(operator)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid operator UUID")
            conditions.append(f"a.operator_id = ${idx}")
            params.append(op_uuid)
            idx += 1

        if status is not None:
            if status not in ("active", "departed"):
                raise HTTPException(status_code=422, detail="status must be 'active' or 'departed'")
            conditions.append(f"a.status = ${idx}")
            params.append(status)
            idx += 1

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        rows = await conn.fetch(
            f"""
            SELECT a.id, a.name, a.persona, a.status, a.created_at, a.departed_at,
                   aa.address, o.username AS operator_username
            FROM agents a
            LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
            JOIN operators o ON o.id = a.operator_id
            {where}
            ORDER BY a.created_at DESC
            """,
            *params,
        )

    return {
        "agents": [
            {
                "uuid": str(r["id"]),
                "address": r["address"],
                "display_name": r["name"],
                "status": r["status"],
                "operator_username": r["operator_username"],
                "created_at": r["created_at"],
                "departed_at": r["departed_at"],
            }
            for r in rows
        ]
    }


@router.post("/{agent_id}/depart", dependencies=[Depends(_require_admin)])
async def admin_depart_agent(agent_id: str):
    """Admin force-depart an agent (no ownership check)."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)

    async with get_conn() as conn:
        try:
            result = await do_depart(conn, agent_uuid)
        except ValueError as e:
            detail = str(e)
            code = 404 if "not found" in detail else 409
            raise HTTPException(status_code=code, detail=detail)

        addr_row = await conn.fetchrow(
            "SELECT address FROM agent_addresses WHERE agent_id = $1", agent_uuid
        )

    return {
        "uuid": str(agent_uuid),
        "address": addr_row["address"] if addr_row else None,
        "status": "departed",
        "capabilities_revoked": result["capabilities_revoked"],
        "departed_at": result["departed_at"],
        "data_expires_at": result["data_expires_at"],
    }


@router.post("/{agent_id}/reinstate", dependencies=[Depends(_require_admin)])
async def admin_reinstate_agent(agent_id: str):
    """Admin reinstate a departed agent (no ownership check)."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)

    async with get_conn() as conn:
        try:
            result = await do_reinstate(conn, agent_uuid)
        except ValueError as e:
            detail = str(e)
            if "not found" in detail:
                code = 404
            elif "Cannot reinstate" in detail:
                code = 409
            else:
                code = 409
            raise HTTPException(status_code=code, detail=detail)

        addr_row = await conn.fetchrow(
            "SELECT address FROM agent_addresses WHERE agent_id = $1", agent_uuid
        )

    return {
        "uuid": str(agent_uuid),
        "address": addr_row["address"] if addr_row else None,
        "status": "active",
        "message": "Agent reinstated. Previously revoked capabilities must be re-granted.",
    }
