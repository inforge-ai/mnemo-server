from fastapi import APIRouter, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from ..database import get_conn
from ..services.auth_service import create_agent_with_key, validate_api_key

router = APIRouter(tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


class RegisterRequest(BaseModel):
    name: str
    persona: str = ""
    domain_tags: list[str] = []
    key_name: str = "default"


class RegisterResponse(BaseModel):
    agent_id: str
    name: str
    api_key: str
    message: str


@router.post("/auth/register", response_model=RegisterResponse, status_code=201)
async def register(body: RegisterRequest):
    """
    Create an agent (or add a key to an existing one) and return the API key.
    The key is returned exactly once — it is not stored in plaintext.
    """
    async with get_conn() as conn:
        agent, plaintext_key = await create_agent_with_key(
            conn=conn,
            name=body.name,
            persona=body.persona,
            domain_tags=body.domain_tags,
            key_name=body.key_name,
        )
    return {
        "agent_id": agent["id"],
        "name": agent["name"],
        "api_key": plaintext_key,
        "message": "Save this key — it will not be shown again.",
    }


@router.get("/auth/me")
async def me(credentials: HTTPAuthorizationCredentials | None = Security(_bearer)):
    """
    Return info about the currently authenticated agent.
    Always requires a valid Bearer token regardless of MNEMO_AUTH_ENABLED.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    async with get_conn() as conn:
        agent = await validate_api_key(conn, credentials.credentials)

    if agent is None:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    return agent
