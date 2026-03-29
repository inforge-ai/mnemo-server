"""
RBAC-Lite auth middleware.

Three credential types resolved in order:
  1. X-Admin-Key  → role="admin"
  2. X-Agent-Key  → role="agent"  (data-plane: remember, recall, share, stats)
  3. X-Operator-Key → role="operator" (management-plane: register agent, inspect shares)
"""

import secrets
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Request

from .config import settings
from .database import get_conn
from .services.auth_service import hash_key, validate_agent_key, validate_api_key


@dataclass
class AuthContext:
    role: Literal["admin", "operator", "agent"]
    operator_id: UUID | None = None
    agent_id: UUID | None = None
    operator_name: str | None = None



async def resolve_auth(request: Request) -> AuthContext:
    """
    Resolve credentials from request headers.

    Priority: X-Admin-Key > X-Agent-Key > X-Operator-Key.
    No fallback between key types — wrong key type returns 401.
    """
    # 1. Admin key (accepts X-Admin-Key or legacy X-Admin-Token)
    admin_key = request.headers.get("X-Admin-Key") or request.headers.get("X-Admin-Token")
    if admin_key:
        if settings.admin_key and secrets.compare_digest(admin_key, settings.admin_key):
            return AuthContext(role="admin")
        raise HTTPException(status_code=401, detail="Invalid admin key")

    # 2. Agent key (data-plane)
    agent_key = request.headers.get("X-Agent-Key")
    if agent_key:
        async with get_conn() as conn:
            agent = await validate_agent_key(conn, agent_key)
        if agent is None:
            raise HTTPException(status_code=401, detail="Invalid or inactive agent key")
        return AuthContext(
            role="agent",
            operator_id=agent["operator_id"],
            agent_id=agent["agent_id"],
            operator_name=agent["operator_name"],
        )

    # 3. Operator key (management-plane)
    operator_key = request.headers.get("X-Operator-Key")
    if operator_key:
        async with get_conn() as conn:
            operator = await validate_api_key(conn, operator_key)
        if operator is None:
            raise HTTPException(status_code=401, detail="Invalid or inactive operator key")
        if operator.get("status") != "active":
            raise HTTPException(
                status_code=403,
                detail=f"Operator account is {operator.get('status', 'unknown')}",
            )
        return AuthContext(
            role="operator",
            operator_id=UUID(operator["id"]),
            operator_name=operator["name"],
        )

    raise HTTPException(status_code=401, detail="Missing credentials. Provide X-Admin-Key, X-Agent-Key, or X-Operator-Key header.")


# ── Role guard dependencies ───────────────────────────────────────────────────


async def require_admin(auth: AuthContext = Depends(resolve_auth)) -> AuthContext:
    if auth.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return auth


async def require_operator(auth: AuthContext = Depends(resolve_auth)) -> AuthContext:
    if auth.role not in ("operator", "admin"):
        raise HTTPException(status_code=403, detail="Operator access required")
    return auth


async def require_agent(auth: AuthContext = Depends(resolve_auth)) -> AuthContext:
    if auth.role not in ("agent", "admin"):
        raise HTTPException(
            status_code=403,
            detail="Agent access required. Use X-Agent-Key header for this endpoint.",
        )
    return auth


def require_agent_match(agent_id: UUID, auth: AuthContext) -> None:
    """Verify the URL agent_id matches the key's agent. Admin bypasses."""
    if auth.role == "admin":
        return
    if auth.agent_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail="Agent key does not match the agent_id in the URL",
        )


# ── Backward-compatible wrapper (used during transition) ─────────────────────


async def get_current_operator(auth: AuthContext = Depends(resolve_auth)) -> dict:
    """
    Legacy wrapper: returns the old-style operator dict.
    Used by routes that haven't been migrated to AuthContext yet.
    """
    if auth.role == "admin":
        return {"id": None, "name": "admin"}

    if auth.operator_id is None:
        raise HTTPException(status_code=401, detail="Missing credentials")

    return {
        "id": str(auth.operator_id),
        "name": auth.operator_name or "unknown",
    }


async def verify_agent_ownership(operator: dict, agent_id: UUID | str) -> None:
    """
    Legacy wrapper: verify that the authenticated operator owns this agent.
    No-op when auth is disabled (operator["id"] is None).
    """
    if isinstance(agent_id, str):
        agent_id = UUID(agent_id)
    if operator["id"] is None:
        return  # admin — skip ownership check

    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM agents
            WHERE id = $1 AND operator_id = $2 AND status = 'active'
            """,
            agent_id,
            UUID(operator["id"]),
        )

    if not row:
        raise HTTPException(
            status_code=403,
            detail="Agent not found or not owned by this operator",
        )
