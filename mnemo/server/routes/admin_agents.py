"""
Admin agent endpoints — protected by X-Admin-Token header.

Endpoints:
  GET    /v1/admin/agents                        — list all agents
  POST   /v1/admin/agents/{agent_id}/depart      — admin force-depart
  POST   /v1/admin/agents/{agent_id}/reinstate   — admin reinstate
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

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
            SELECT a.id, a.name, a.persona, a.domain_tags,
                   a.status, a.created_at, a.departed_at,
                   aa.address, o.username AS operator_username,
                   COUNT(DISTINCT at.id) FILTER (WHERE at.is_active)  AS active_atoms,
                   COUNT(DISTINCT at.id)                               AS total_atoms,
                   COUNT(DISTINCT k.id)  FILTER (WHERE k.is_active)   AS active_keys
            FROM agents a
            LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
            JOIN operators o ON o.id = a.operator_id
            LEFT JOIN atoms at ON at.agent_id = a.id
            LEFT JOIN api_keys k ON k.operator_id = a.operator_id
            {where}
            GROUP BY a.id, aa.address, o.username
            ORDER BY a.created_at DESC
            """,
            *params,
        )

    return {
        "agents": [
            {
                "uuid": str(r["id"]),
                "id": str(r["id"]),
                "address": r["address"],
                "display_name": r["name"],
                "name": r["name"],
                "persona": r["persona"],
                "domain_tags": list(r["domain_tags"]) if r["domain_tags"] else [],
                "status": r["status"],
                "operator_username": r["operator_username"],
                "created_at": r["created_at"],
                "departed_at": r["departed_at"],
                "active_atoms": r["active_atoms"],
                "total_atoms": r["total_atoms"],
                "active_keys": r["active_keys"],
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


class PurgeConfirmation(BaseModel):
    confirm: str


@router.post("/{agent_id}/purge", dependencies=[Depends(_require_admin)])
async def admin_purge_agent(agent_id: str, body: PurgeConfirmation):
    """Admin hard-purge: permanently delete all agent data and mark departed."""
    if body.confirm != "purge":
        raise HTTPException(status_code=422, detail='Request body must be {"confirm": "purge"}')

    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)

    async with get_conn() as conn:
        async with conn.transaction():
            # a) Revoke all outbound capabilities
            revoked_tag = await conn.execute(
                "UPDATE capabilities SET revoked = true, revoked_at = now() "
                "WHERE grantor_id = $1 AND revoked = false",
                agent_uuid,
            )
            shares_revoked = int(revoked_tag.split()[-1])

            # b) Delete all inbound capabilities
            await conn.execute(
                "DELETE FROM capabilities WHERE grantee_id = $1",
                agent_uuid,
            )

            # c) Get list of atom IDs for this agent
            atom_rows = await conn.fetch(
                "SELECT id FROM atoms WHERE agent_id = $1",
                agent_uuid,
            )
            atom_ids = [r["id"] for r in atom_rows]

            edges_deleted = 0
            if atom_ids:
                # d) Delete all edges where source_id or target_id is in atom_ids
                edges_tag = await conn.execute(
                    "DELETE FROM edges WHERE source_id = ANY($1) OR target_id = ANY($1)",
                    atom_ids,
                )
                edges_deleted = int(edges_tag.split()[-1])

                # e) Delete all snapshot_atoms where atom_id is in atom_ids
                await conn.execute(
                    "DELETE FROM snapshot_atoms WHERE atom_id = ANY($1)",
                    atom_ids,
                )

            # f) Delete all views owned by this agent
            await conn.execute(
                "DELETE FROM views WHERE owner_agent_id = $1",
                agent_uuid,
            )

            # g) Hard-delete all atoms
            atoms_tag = await conn.execute(
                "DELETE FROM atoms WHERE agent_id = $1",
                agent_uuid,
            )
            atoms_deleted = int(atoms_tag.split()[-1])

            # h) Set agent status to departed
            await conn.execute(
                "UPDATE agents SET status = 'departed', departed_at = now(), "
                "data_expires_at = now() + interval '30 days' "
                "WHERE id = $1",
                agent_uuid,
            )

            addr_row = await conn.fetchrow(
                "SELECT address FROM agent_addresses WHERE agent_id = $1",
                agent_uuid,
            )

    return {
        "agent_id": str(agent_uuid),
        "address": addr_row["address"] if addr_row else None,
        "atoms_deleted": atoms_deleted,
        "edges_deleted": edges_deleted,
        "shares_revoked": shares_revoked,
        "status": "departed",
    }
