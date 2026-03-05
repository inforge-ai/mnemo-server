from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .database import get_conn
from .services.auth_service import validate_api_key

security = HTTPBearer(auto_error=False)

# Sentinel returned when auth is disabled
_UNAUTHED_SENTINEL = {"id": None}


async def get_current_agent(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> dict:
    """
    Validates Bearer token against api_keys table.
    Returns agent dict on success.
    Raises 401 if MNEMO_AUTH_ENABLED and key is missing or invalid.
    If MNEMO_AUTH_ENABLED=false, returns a sentinel agent dict with id=None.
    """
    if not settings.auth_enabled:
        return _UNAUTHED_SENTINEL

    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    async with get_conn() as conn:
        agent = await validate_api_key(conn, credentials.credentials)

    if agent is None:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    return agent
