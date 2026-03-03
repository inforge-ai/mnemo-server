import json
from uuid import UUID

from fastapi import APIRouter, HTTPException

from ..database import get_conn
from ..models import GrantCreate, CapabilityResponse

router = APIRouter(tags=["capabilities"])


@router.post("/agents/{agent_id}/grant", response_model=CapabilityResponse, status_code=201)
async def grant_capability(agent_id: UUID, body: GrantCreate):
    """
    Grant another agent access to one of your views.
    grantor must own the view.
    """
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)

        # Verify the grantor owns the view
        view_owner = await conn.fetchval(
            "SELECT owner_agent_id FROM views WHERE id = $1",
            body.view_id,
        )
        if view_owner is None:
            raise HTTPException(status_code=404, detail="View not found")
        if view_owner != agent_id:
            raise HTTPException(status_code=403, detail="Not view owner")

        # Verify grantee exists and is active
        grantee = await conn.fetchrow(
            "SELECT is_active FROM agents WHERE id = $1",
            body.grantee_id,
        )
        if not grantee:
            raise HTTPException(status_code=404, detail="Grantee agent not found")
        if not grantee["is_active"]:
            raise HTTPException(status_code=410, detail="Grantee agent has departed")

        row = await conn.fetchrow(
            """
            INSERT INTO capabilities
                (view_id, grantor_id, grantee_id, permissions, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, view_id, grantor_id, grantee_id, permissions,
                      revoked, expires_at, created_at
            """,
            body.view_id,
            agent_id,
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
            agent_id,
            row["id"],
            json.dumps({"grantee_id": str(body.grantee_id), "view_id": str(body.view_id)}),
        )

    return _cap_row(row)


@router.post("/capabilities/{cap_id}/revoke")
async def revoke_capability(cap_id: UUID):
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

        # Cascade revoke via recursive CTE
        revoked_count = await conn.fetchval(
            """
            WITH RECURSIVE cap_tree AS (
                SELECT id FROM capabilities WHERE id = $1 AND revoked = false
                UNION
                SELECT c.id FROM capabilities c
                JOIN cap_tree ct ON c.parent_cap_id = ct.id
                WHERE c.revoked = false
            )
            UPDATE capabilities SET revoked = true
            WHERE id IN (SELECT id FROM cap_tree)
            RETURNING id
            """,
            cap_id,
        )

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
