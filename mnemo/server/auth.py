from uuid import UUID

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .database import get_conn
from .services.auth_service import validate_api_key

security = HTTPBearer(auto_error=False)

# Sentinel returned when auth is disabled
_UNAUTHED_SENTINEL = {"id": None, "name": "anonymous"}


async def get_current_operator(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> dict:
    """
    Validates Bearer token against api_keys table.
    Returns operator dict on success.
    Raises 401 if MNEMO_AUTH_ENABLED and key is missing or invalid.
    If MNEMO_AUTH_ENABLED=false, returns a sentinel operator dict with id=None.
    """
    if not settings.auth_enabled:
        return _UNAUTHED_SENTINEL

    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    async with get_conn() as conn:
        operator = await validate_api_key(conn, credentials.credentials)

    if operator is None:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    return operator


async def verify_agent_ownership(operator: dict, agent_id: UUID) -> None:
    """
    Verify that the authenticated operator owns this agent.
    No-op when auth is disabled (operator["id"] is None).
    Raises 403 if operator doesn't own the agent.
    """
    if operator["id"] is None:
        return  # auth disabled — skip ownership check

    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM agents
            WHERE id = $1 AND operator_id = $2 AND is_active = true
            """,
            agent_id,
            UUID(operator["id"]),
        )

    if not row:
        raise HTTPException(
            status_code=403,
            detail="Agent not found or not owned by this operator",
        )
