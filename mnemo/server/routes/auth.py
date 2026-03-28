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
