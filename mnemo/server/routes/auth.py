import asyncpg
from fastapi import APIRouter, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from ..database import get_conn
from ..services.auth_service import (
    create_operator_key,
    create_operator_with_key,
    validate_api_key,
)

router = APIRouter(tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


class RegisterOperatorRequest(BaseModel):
    name: str
    email: str | None = None
    username: str | None = None
    org: str = "mnemo"


class RegisterOperatorResponse(BaseModel):
    operator_id: str
    name: str
    api_key: str
    message: str


class NewKeyResponse(BaseModel):
    operator_id: str
    api_key: str
    message: str


@router.post("/auth/register-operator", response_model=RegisterOperatorResponse, status_code=201)
async def register_operator(body: RegisterOperatorRequest):
    """
    Create an operator and return its API key.
    The key is returned exactly once — it is not stored in plaintext.
    """
    try:
        async with get_conn() as conn:
            operator, plaintext_key = await create_operator_with_key(
                conn=conn,
                name=body.name,
                email=body.email,
                username=body.username,
                org=body.org,
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail=f"Operator name '{body.name}' already exists")
    return {
        "operator_id": operator["id"],
        "name": operator["name"],
        "api_key": plaintext_key,
        "message": "Save this key — it will not be shown again.",
    }


@router.post("/auth/new-key", response_model=NewKeyResponse)
async def new_key(credentials: HTTPAuthorizationCredentials | None = Security(_bearer)):
    """
    Generate an additional API key for the authenticated operator.
    Returns the new key once. Old keys still work.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    async with get_conn() as conn:
        operator = await validate_api_key(conn, credentials.credentials)
        if operator is None:
            raise HTTPException(status_code=401, detail="Invalid or inactive API key")

        from uuid import UUID
        plaintext_key = await create_operator_key(conn, UUID(operator["id"]))

    return {
        "operator_id": operator["id"],
        "api_key": plaintext_key,
        "message": "Save this key — it will not be shown again.",
    }


@router.get("/auth/me")
async def me(credentials: HTTPAuthorizationCredentials | None = Security(_bearer)):
    """
    Return info about the currently authenticated operator.
    Always requires a valid Bearer token regardless of MNEMO_AUTH_ENABLED.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    async with get_conn() as conn:
        operator = await validate_api_key(conn, credentials.credentials)

    if operator is None:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    # Count agents under this operator
    from uuid import UUID
    async with get_conn() as conn:
        agent_count = await conn.fetchval(
            "SELECT COUNT(*) FROM agents WHERE operator_id = $1 AND status = 'active'",
            UUID(operator["id"]),
        )

    return {
        "id": operator["id"],
        "name": operator["name"],
        "email": operator["email"],
        "agent_count": agent_count,
        "key_prefix": operator["key_prefix"],
    }
