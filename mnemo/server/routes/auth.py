from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import AuthContext, require_admin, require_operator
from ..database import get_conn
from ..services.auth_service import create_operator_key

router = APIRouter(tags=["auth"])


class NewKeyResponse(BaseModel):
    operator_id: str
    api_key: str
    message: str


class KeyInfo(BaseModel):
    id: str
    key_prefix: str
    name: str | None
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None


@router.post("/auth/new-key", response_model=NewKeyResponse)
async def new_key(auth: AuthContext = Depends(require_operator)):
    """
    Generate an additional API key for the authenticated operator.
    Returns the new key once. Old keys still work.
    """
    if auth.operator_id is None:
        raise HTTPException(status_code=400, detail="Cannot generate key without operator context")

    async with get_conn() as conn:
        plaintext_key = await create_operator_key(conn, auth.operator_id)

    return {
        "operator_id": str(auth.operator_id),
        "api_key": plaintext_key,
        "message": "Save this key — it will not be shown again.",
    }


@router.get("/auth/me")
async def me(auth: AuthContext = Depends(require_operator)):
    """
    Return info about the currently authenticated operator.
    """
    if auth.operator_id is None:
        return {"role": auth.role, "message": "Admin account (no operator context)"}

    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT COUNT(a.id) AS agent_count, o.sharing_scope
            FROM operators o
            LEFT JOIN agents a ON a.operator_id = o.id AND a.status = 'active'
            WHERE o.id = $1
            GROUP BY o.id
            """,
            auth.operator_id,
        )

    return {
        "id": str(auth.operator_id),
        "name": auth.operator_name,
        "role": auth.role,
        "agent_count": row["agent_count"] if row else 0,
        "sharing_scope": row["sharing_scope"] if row else "none",
    }


@router.get("/auth/keys", response_model=list[KeyInfo])
async def list_keys(auth: AuthContext = Depends(require_operator)):
    """List all API keys (active and revoked) for the authenticated operator."""
    async with get_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT id, key_prefix, name, is_active, created_at, last_used_at
            FROM api_keys
            WHERE operator_id = $1
            ORDER BY created_at DESC
            """,
            auth.operator_id,
        )
    return [
        {
            "id": str(r["id"]),
            "key_prefix": r["key_prefix"],
            "name": r["name"],
            "is_active": r["is_active"],
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"],
        }
        for r in rows
    ]


@router.delete("/auth/keys/{key_id}", status_code=200)
async def revoke_key(key_id: UUID, auth: AuthContext = Depends(require_operator)):
    """Revoke a specific API key. Cannot revoke the last active key."""
    async with get_conn() as conn:
        row = await conn.fetchrow(
            "SELECT id, is_active FROM api_keys WHERE id = $1 AND operator_id = $2",
            key_id,
            auth.operator_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Key not found")
        if not row["is_active"]:
            raise HTTPException(status_code=409, detail="Key is already revoked")

        active_count = await conn.fetchval(
            "SELECT COUNT(*) FROM api_keys WHERE operator_id = $1 AND is_active = true",
            auth.operator_id,
        )
        if active_count <= 1:
            raise HTTPException(
                status_code=409,
                detail="Cannot revoke your only active key. Create a new key first.",
            )

        await conn.execute(
            "UPDATE api_keys SET is_active = false WHERE id = $1",
            key_id,
        )
    return {"status": "revoked", "key_id": str(key_id)}
