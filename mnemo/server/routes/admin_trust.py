"""
Admin trust/sharing management endpoints — protected by X-Admin-Token header.

Endpoints:
  GET    /v1/admin/trust/status         — get sharing status
  POST   /v1/admin/trust/enable         — enable sharing globally
  POST   /v1/admin/trust/disable        — disable sharing globally
  GET    /v1/admin/trust/shares         — list active shares/capabilities
  DELETE /v1/admin/trust/shares/{cap_id} — admin revoke share (cascade)
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import get_conn
from .admin import _require_admin

router = APIRouter(tags=["admin"], prefix="/admin/trust")


@router.get("/status", dependencies=[Depends(_require_admin)])
async def trust_status():
    """Get the current sharing enabled/disabled status."""
    async with get_conn() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM platform_config WHERE key = 'sharing_enabled'"
        )
    val = row["value"] if row is not None else None
    enabled = val is True or val == "true"
    return {"sharing_enabled": enabled}


@router.post("/enable", dependencies=[Depends(_require_admin)])
async def trust_enable():
    """Enable sharing globally."""
    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO platform_config (key, value, updated_at)
            VALUES ('sharing_enabled', 'true'::jsonb, now())
            ON CONFLICT (key) DO UPDATE SET value = 'true'::jsonb, updated_at = now()
            """
        )
    return {"sharing_enabled": True}


@router.post("/disable", dependencies=[Depends(_require_admin)])
async def trust_disable():
    """Disable sharing globally. Existing shares are suspended, not deleted."""
    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO platform_config (key, value, updated_at)
            VALUES ('sharing_enabled', 'false'::jsonb, now())
            ON CONFLICT (key) DO UPDATE SET value = 'false'::jsonb, updated_at = now()
            """
        )
    return {
        "sharing_enabled": False,
        "note": "Existing shares suspended, not deleted. Enable to restore.",
    }


@router.get("/shares", dependencies=[Depends(_require_admin)])
async def list_shares(
    operator: str | None = Query(None, description="Filter by operator UUID"),
    agent: str | None = Query(None, description="Filter by agent UUID (grantor or grantee)"),
):
    """List active (non-revoked, non-expired) shares/capabilities."""
    conditions = ["c.revoked = false", "(c.expires_at IS NULL OR c.expires_at > now())"]
    params = []
    idx = 1

    if operator is not None:
        try:
            op_uuid = UUID(operator)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid operator UUID")
        conditions.append(
            f"(grantor_agent.operator_id = ${idx} OR grantee_agent.operator_id = ${idx})"
        )
        params.append(op_uuid)
        idx += 1

    if agent is not None:
        try:
            agent_uuid = UUID(agent)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid agent UUID")
        conditions.append(f"(c.grantor_id = ${idx} OR c.grantee_id = ${idx})")
        params.append(agent_uuid)
        idx += 1

    where = "WHERE " + " AND ".join(conditions)

    async with get_conn() as conn:
        rows = await conn.fetch(
            f"""
            SELECT c.id AS capability_id,
                   grantor_addr.address AS grantor_address,
                   grantee_addr.address AS grantee_address,
                   v.name AS view_name,
                   c.created_at,
                   (SELECT COUNT(*) FROM snapshot_atoms sa WHERE sa.view_id = v.id) AS atom_count
            FROM capabilities c
            JOIN views v ON v.id = c.view_id
            JOIN agents grantor_agent ON grantor_agent.id = c.grantor_id
            JOIN agents grantee_agent ON grantee_agent.id = c.grantee_id
            LEFT JOIN agent_addresses grantor_addr ON grantor_addr.agent_id = c.grantor_id
            LEFT JOIN agent_addresses grantee_addr ON grantee_addr.agent_id = c.grantee_id
            {where}
            ORDER BY c.created_at DESC
            """,
            *params,
        )

    return {
        "shares": [
            {
                "capability_id": str(r["capability_id"]),
                "grantor_address": r["grantor_address"],
                "grantee_address": r["grantee_address"],
                "view_name": r["view_name"],
                "created_at": r["created_at"],
                "atom_count": r["atom_count"],
            }
            for r in rows
        ]
    }


@router.delete("/shares/{capability_id}", dependencies=[Depends(_require_admin)])
async def admin_revoke_share(capability_id: UUID):
    """Admin revoke a share with cascade through capability tree."""
    async with get_conn() as conn:
        cap = await conn.fetchrow(
            "SELECT id, grantor_id, revoked FROM capabilities WHERE id = $1",
            capability_id,
        )
        if not cap:
            raise HTTPException(status_code=404, detail="Capability not found")
        if cap["revoked"]:
            raise HTTPException(status_code=404, detail="Capability already revoked")

        # Cascade revoke via recursive CTE
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
            capability_id,
        )
        revoked_count = len(revoked_rows)

        await conn.execute(
            """
            INSERT INTO access_log (agent_id, action, target_id, metadata)
            VALUES ($1, 'admin_revoke', $2, $3)
            """,
            cap["grantor_id"],
            capability_id,
            json.dumps({"cascade_revoked": revoked_count, "admin": True}),
        )

    return {
        "capability_id": str(capability_id),
        "revoked": True,
        "cascade_count": revoked_count,
    }
