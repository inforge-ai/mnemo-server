"""
Share management endpoints (RBAC-Lite).

Operator-level endpoints for inspecting and blocking/unblocking inbound shares.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ..auth import AuthContext, require_operator
from ..database import get_conn

router = APIRouter(tags=["shares"])


@router.get("/operators/me/shares")
async def inspect_shares(auth: AuthContext = Depends(require_operator)):
    """List all inbound and outbound shares for this operator's agents."""
    if auth.operator_id is None:
        raise HTTPException(status_code=400, detail="No operator context")

    async with get_conn() as conn:
        # Get all agent IDs belonging to this operator
        agent_rows = await conn.fetch(
            "SELECT id FROM agents WHERE operator_id = $1 AND status = 'active'",
            auth.operator_id,
        )
        agent_ids = [r["id"] for r in agent_rows]

        if not agent_ids:
            return {"inbound": [], "outbound": []}

        # Inbound: capabilities where grantee is one of our agents
        inbound_rows = await conn.fetch(
            """
            SELECT c.id AS capability_id,
                   grantor_aa.address AS grantor_address,
                   grantee_aa.address AS grantee_address,
                   v.name AS view_name,
                   COUNT(sa.atom_id) AS atom_count,
                   c.blocked_by_recipient AS blocked,
                   c.created_at
            FROM capabilities c
            JOIN views v ON v.id = c.view_id
            LEFT JOIN snapshot_atoms sa ON sa.view_id = v.id
            LEFT JOIN agent_addresses grantor_aa ON grantor_aa.agent_id = c.grantor_id
            LEFT JOIN agent_addresses grantee_aa ON grantee_aa.agent_id = c.grantee_id
            WHERE c.grantee_id = ANY($1)
              AND c.revoked = false
              AND (c.expires_at IS NULL OR c.expires_at > now())
            GROUP BY c.id, grantor_aa.address, grantee_aa.address, v.name, c.blocked_by_recipient, c.created_at
            ORDER BY c.created_at DESC
            """,
            agent_ids,
        )

        # Outbound: capabilities where grantor is one of our agents
        outbound_rows = await conn.fetch(
            """
            SELECT c.id AS capability_id,
                   grantor_aa.address AS grantor_address,
                   grantee_aa.address AS grantee_address,
                   v.name AS view_name,
                   COUNT(sa.atom_id) AS atom_count,
                   c.blocked_by_recipient AS blocked,
                   c.created_at
            FROM capabilities c
            JOIN views v ON v.id = c.view_id
            LEFT JOIN snapshot_atoms sa ON sa.view_id = v.id
            LEFT JOIN agent_addresses grantor_aa ON grantor_aa.agent_id = c.grantor_id
            LEFT JOIN agent_addresses grantee_aa ON grantee_aa.agent_id = c.grantee_id
            WHERE c.grantor_id = ANY($1)
              AND c.revoked = false
              AND (c.expires_at IS NULL OR c.expires_at > now())
            GROUP BY c.id, grantor_aa.address, grantee_aa.address, v.name, c.blocked_by_recipient, c.created_at
            ORDER BY c.created_at DESC
            """,
            agent_ids,
        )

    def _share_row(r):
        return {
            "capability_id": str(r["capability_id"]),
            "grantor_address": r["grantor_address"],
            "grantee_address": r["grantee_address"],
            "view_name": r["view_name"],
            "atom_count": r["atom_count"],
            "blocked": r["blocked"],
            "created_at": r["created_at"],
        }

    return {
        "inbound": [_share_row(r) for r in inbound_rows],
        "outbound": [_share_row(r) for r in outbound_rows],
    }


@router.post("/shares/{capability_id}/block")
async def block_share(capability_id: UUID, auth: AuthContext = Depends(require_operator)):
    """Block an inbound share to one of this operator's agents."""
    if auth.operator_id is None:
        raise HTTPException(status_code=400, detail="No operator context")

    async with get_conn() as conn:
        # Verify the capability exists and grantee belongs to this operator
        row = await conn.fetchrow(
            """
            SELECT c.id, c.blocked_by_recipient, a.operator_id
            FROM capabilities c
            JOIN agents a ON a.id = c.grantee_id
            WHERE c.id = $1 AND c.revoked = false
            """,
            capability_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Capability not found or already revoked")
        if row["operator_id"] != auth.operator_id:
            raise HTTPException(status_code=403, detail="Capability grantee does not belong to your operator")

        await conn.execute(
            "UPDATE capabilities SET blocked_by_recipient = TRUE WHERE id = $1",
            capability_id,
        )

    return {
        "capability_id": str(capability_id),
        "blocked": True,
        "note": "Inbound share blocked. Agent will no longer see these shared memories.",
    }


@router.post("/shares/{capability_id}/unblock")
async def unblock_share(capability_id: UUID, auth: AuthContext = Depends(require_operator)):
    """Unblock a previously blocked inbound share."""
    if auth.operator_id is None:
        raise HTTPException(status_code=400, detail="No operator context")

    async with get_conn() as conn:
        # Verify the capability exists and grantee belongs to this operator
        row = await conn.fetchrow(
            """
            SELECT c.id, c.blocked_by_recipient, a.operator_id
            FROM capabilities c
            JOIN agents a ON a.id = c.grantee_id
            WHERE c.id = $1 AND c.revoked = false
            """,
            capability_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Capability not found or already revoked")
        if row["operator_id"] != auth.operator_id:
            raise HTTPException(status_code=403, detail="Capability grantee does not belong to your operator")

        await conn.execute(
            "UPDATE capabilities SET blocked_by_recipient = FALSE WHERE id = $1",
            capability_id,
        )

    return {
        "capability_id": str(capability_id),
        "blocked": False,
        "note": "Inbound share unblocked. Agent can now see these shared memories again.",
    }
