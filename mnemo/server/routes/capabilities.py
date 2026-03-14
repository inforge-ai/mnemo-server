import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_operator, verify_agent_ownership
from ..database import get_conn, get_pool
from ..services.address_service import resolve_agent_identifier
from ..models import CapabilityResponse, GrantCreate, OutboundCapabilityResponse, RevokeResponse
from ..services import view_service

router = APIRouter(tags=["capabilities"])


@router.post("/agents/{agent_id}/grant", response_model=CapabilityResponse, status_code=201)
async def grant_capability(agent_id: str, body: GrantCreate, operator=Depends(get_current_operator)):
    """
    Grant another agent access to one of your views.
    grantor must own the view.
    """
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)

        # Verify the grantor owns the view
        view_owner = await conn.fetchval(
            "SELECT owner_agent_id FROM views WHERE id = $1",
            body.view_id,
        )
        if view_owner is None:
            raise HTTPException(status_code=404, detail="View not found")
        if view_owner != agent_uuid:
            raise HTTPException(status_code=403, detail="Not view owner")

        # Validate expires_at is in the future
        if body.expires_at and body.expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=422, detail="expires_at must be in the future")

        # Verify grantee exists and is active
        grantee = await conn.fetchrow(
            "SELECT is_active FROM agents WHERE id = $1",
            body.grantee_id,
        )
        if not grantee:
            raise HTTPException(status_code=404, detail="Grantee agent not found")
        if not grantee["is_active"]:
            raise HTTPException(status_code=410, detail="Grantee agent has departed")

        # Idempotency: return existing non-revoked capability for same view+grantee
        existing = await conn.fetchrow(
            """
            SELECT id, view_id, grantor_id, grantee_id, permissions,
                   revoked, expires_at, created_at
            FROM capabilities
            WHERE view_id = $1 AND grantee_id = $2 AND revoked = false
            LIMIT 1
            """,
            body.view_id,
            body.grantee_id,
        )
        if existing:
            return _cap_row(existing)

        row = await conn.fetchrow(
            """
            INSERT INTO capabilities
                (view_id, grantor_id, grantee_id, permissions, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, view_id, grantor_id, grantee_id, permissions,
                      revoked, expires_at, created_at
            """,
            body.view_id,
            agent_uuid,
            body.grantee_id,
            body.permissions,
            body.expires_at,
        )

        # Audit log
        await conn.execute(
            """
            INSERT INTO access_log (agent_id, action, target_id, metadata)
            VALUES ($1, 'grant', $2, $3)
            """,
            agent_uuid,
            row["id"],
            json.dumps({"grantee_id": str(body.grantee_id), "view_id": str(body.view_id)}),
        )

    return _cap_row(row)


@router.post("/capabilities/{cap_id}/revoke")
async def revoke_capability(cap_id: UUID, operator=Depends(get_current_operator)):
    """
    Revoke a capability and all capabilities derived from it (cascade).
    The revoke cascades through the capability tree via a recursive CTE.
    """
    async with get_conn() as conn:
        cap = await conn.fetchrow(
            "SELECT id, grantor_id, revoked FROM capabilities WHERE id = $1",
            cap_id,
        )
        if not cap:
            raise HTTPException(status_code=404, detail="Capability not found")
        if cap["revoked"]:
            raise HTTPException(status_code=409, detail="Capability already revoked")

        # Auth check: operator must own the grantor agent
        await verify_agent_ownership(operator, cap["grantor_id"])

        # Cascade revoke via recursive CTE; fetch returns one row per revoked cap
        revoked_rows = await conn.fetch(
            """
            WITH RECURSIVE cap_tree AS (
                SELECT id FROM capabilities WHERE id = $1 AND revoked = false
                UNION
                SELECT c.id FROM capabilities c
                JOIN cap_tree ct ON c.parent_cap_id = ct.id
                WHERE c.revoked = false
            )
            UPDATE capabilities SET revoked = true, revoked_at = now()
            WHERE id IN (SELECT id FROM cap_tree)
            RETURNING id
            """,
            cap_id,
        )
        revoked_count = len(revoked_rows)

        await conn.execute(
            """
            INSERT INTO access_log (agent_id, action, target_id, metadata)
            VALUES ($1, 'revoke', $2, $3)
            """,
            cap["grantor_id"],
            cap_id,
            json.dumps({"cascade_revoked": revoked_count}),
        )

    return {"revoked": True, "cascade_revoked": revoked_count}


@router.post(
    "/agents/{agent_id}/capabilities/{capability_id}/revoke",
    response_model=RevokeResponse,
)
async def revoke_shared_view(
    agent_id: str, capability_id: UUID, operator=Depends(get_current_operator)
):
    """Revoke a shared view capability. Idempotent — revoking already-revoked returns success."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        result = await view_service.revoke_shared_view(
            conn=conn,
            grantor_id=agent_uuid,
            capability_id=capability_id,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="Capability not found or not owned by this agent")
    return result


@router.get(
    "/agents/{agent_id}/capabilities",
    response_model=list[OutboundCapabilityResponse],
)
async def list_outbound_capabilities(
    agent_id: str,
    direction: str = "outbound",
    operator=Depends(get_current_operator),
):
    """List capabilities granted by this agent (outbound)."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        rows = await conn.fetch(
            """
            SELECT c.id AS capability_id, c.view_id, v.name AS view_name,
                   c.grantee_id, aa.address AS grantee_address,
                   c.permissions, c.revoked, c.revoked_at, c.created_at AS granted_at
            FROM capabilities c
            JOIN views v ON v.id = c.view_id
            LEFT JOIN agent_addresses aa ON aa.agent_id = c.grantee_id
            WHERE c.grantor_id = $1
            ORDER BY c.created_at DESC
            """,
            agent_uuid,
        )
    return [
        {
            "capability_id": r["capability_id"],
            "view_id": r["view_id"],
            "view_name": r["view_name"],
            "grantee_id": r["grantee_id"],
            "grantee_address": r["grantee_address"],
            "permissions": list(r["permissions"]),
            "revoked": r["revoked"],
            "revoked_at": r["revoked_at"],
            "granted_at": r["granted_at"],
        }
        for r in rows
    ]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cap_row(row) -> dict:
    return {
        "id": row["id"],
        "view_id": row["view_id"],
        "grantor_id": row["grantor_id"],
        "grantee_id": row["grantee_id"],
        "permissions": list(row["permissions"]),
        "revoked": row["revoked"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
    }


async def _require_active_agent(conn, agent_id: UUID):
    row = await conn.fetchrow(
        "SELECT is_active FROM agents WHERE id = $1", agent_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not row["is_active"]:
        raise HTTPException(status_code=410, detail="Agent has departed")
