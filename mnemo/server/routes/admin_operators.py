"""
Admin operator CRUD endpoints — protected by X-Admin-Token header.

Endpoints:
  POST   /v1/admin/operators                        — create operator
  GET    /v1/admin/operators                        — list all operators
  GET    /v1/admin/operators/{operator_id}          — get single operator
  POST   /v1/admin/operators/{operator_id}/suspend  — suspend operator + depart agents
  POST   /v1/admin/operators/{operator_id}/reinstate — reinstate suspended operator
  POST   /v1/admin/operators/{operator_id}/rotate-key — rotate API keys
"""

import re
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..database import get_conn
from ..services.agent_service import depart_agent as do_depart
from ..services.auth_service import create_operator_with_key, create_operator_key
from .admin import _require_admin

router = APIRouter(tags=["admin"], prefix="/admin/operators")

_USERNAME_RE = re.compile(r"^[a-z][a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")


class OperatorCreateRequest(BaseModel):
    username: str
    org: str
    display_name: str
    email: str
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None


# ── Create operator ──────────────────────────────────────────────────────────

@router.post("", status_code=201, dependencies=[Depends(_require_admin)])
async def create_operator(body: OperatorCreateRequest):
    """Create a new operator with an initial API key."""
    if not _USERNAME_RE.match(body.username):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid username '{body.username}': must match ^[a-z][a-z0-9](?:[a-z0-9-]{{0,28}}[a-z0-9])?$",
        )
    if not _USERNAME_RE.match(body.org):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid org '{body.org}': must match ^[a-z][a-z0-9](?:[a-z0-9-]{{0,28}}[a-z0-9])?$",
        )

    try:
        async with get_conn() as conn:
            operator, plaintext_key = await create_operator_with_key(
                conn,
                name=body.display_name,
                email=body.email,
                username=body.username,
                org=body.org,
                key_name="default",
                stripe_customer_id=body.stripe_customer_id,
                stripe_subscription_id=body.stripe_subscription_id,
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail=f"Operator with username '{body.username}' or display_name '{body.display_name}' already exists",
        )

    return {
        "uuid": operator["id"],
        "username": operator["username"],
        "org": operator["org"],
        "display_name": operator["name"],
        "email": operator["email"],
        "api_key": plaintext_key,
        "status": operator["status"],
        "created_at": operator["created_at"],
    }


# ── List operators ───────────────────────────────────────────────────────────

@router.get("", dependencies=[Depends(_require_admin)])
async def list_operators():
    """List all operators with agent counts."""
    async with get_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT o.id, o.name, o.username, o.org, o.email, o.status,
                   o.sharing_scope, o.created_at,
                   COUNT(a.id) AS agent_count
            FROM operators o
            LEFT JOIN agents a ON a.operator_id = o.id
            GROUP BY o.id
            ORDER BY o.created_at DESC
            """
        )
    return {
        "operators": [
            {
                "uuid": str(r["id"]),
                "username": r["username"],
                "org": r["org"],
                "display_name": r["name"],
                "email": r["email"],
                "status": r["status"],
                "sharing_scope": r["sharing_scope"],
                "agent_count": r["agent_count"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


# ── Get single operator ─────────────────────────────────────────────────────

@router.get("/{operator_id}", dependencies=[Depends(_require_admin)])
async def get_operator(operator_id: UUID):
    """Get a single operator by UUID, including their agents."""
    async with get_conn() as conn:
        op = await conn.fetchrow(
            """
            SELECT id, name, username, org, email, status, sharing_scope,
                   stripe_customer_id, stripe_subscription_id,
                   created_at, updated_at
            FROM operators WHERE id = $1
            """,
            operator_id,
        )
        if not op:
            raise HTTPException(status_code=404, detail="Operator not found")

        agents = await conn.fetch(
            """
            SELECT a.id, a.name, a.status, a.created_at, a.departed_at,
                   aa.address
            FROM agents a
            LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
            WHERE a.operator_id = $1
            ORDER BY a.created_at DESC
            """,
            operator_id,
        )

    return {
        "uuid": str(op["id"]),
        "username": op["username"],
        "org": op["org"],
        "display_name": op["name"],
        "email": op["email"],
        "status": op["status"],
        "sharing_scope": op["sharing_scope"],
        "stripe_customer_id": op["stripe_customer_id"],
        "stripe_subscription_id": op["stripe_subscription_id"],
        "created_at": op["created_at"],
        "updated_at": op["updated_at"],
        "agents": [
            {
                "id": str(a["id"]),
                "name": a["name"],
                "status": a["status"],
                "address": a["address"],
                "created_at": a["created_at"],
                "departed_at": a["departed_at"],
            }
            for a in agents
        ],
    }


# ── Suspend operator ────────────────────────────────────────────────────────

@router.post("/{operator_id}/suspend", dependencies=[Depends(_require_admin)])
async def suspend_operator(operator_id: UUID):
    """Suspend an operator and depart all their active agents."""
    async with get_conn() as conn:
        op = await conn.fetchrow(
            "SELECT id, name, username, status FROM operators WHERE id = $1",
            operator_id,
        )
        if not op:
            raise HTTPException(status_code=404, detail="Operator not found")
        if op["status"] != "active":
            raise HTTPException(
                status_code=409,
                detail=f"Operator is already {op['status']}",
            )

        # Suspend the operator
        await conn.execute(
            "UPDATE operators SET status = 'suspended', updated_at = now() WHERE id = $1",
            operator_id,
        )

        # Depart all active agents using shared service
        active_agents = await conn.fetch(
            "SELECT id FROM agents WHERE operator_id = $1 AND status = 'active'",
            operator_id,
        )
        agents_departed = 0
        for agent_row in active_agents:
            try:
                await do_depart(conn, agent_row["id"])
                agents_departed += 1
            except ValueError:
                pass  # skip if already departed

    return {
        "uuid": str(op["id"]),
        "username": op["username"],
        "status": "suspended",
        "agents_departed": agents_departed,
    }


# ── Reinstate operator ──────────────────────────────────────────────────────

@router.post("/{operator_id}/reinstate", dependencies=[Depends(_require_admin)])
async def reinstate_operator(operator_id: UUID):
    """Reinstate a suspended operator. Agents remain departed."""
    async with get_conn() as conn:
        op = await conn.fetchrow(
            "SELECT id, name, username, status FROM operators WHERE id = $1",
            operator_id,
        )
        if not op:
            raise HTTPException(status_code=404, detail="Operator not found")
        if op["status"] != "suspended":
            raise HTTPException(
                status_code=409,
                detail=f"Operator is not suspended (current status: {op['status']})",
            )

        await conn.execute(
            "UPDATE operators SET status = 'active', updated_at = now() WHERE id = $1",
            operator_id,
        )

    return {
        "uuid": str(op["id"]),
        "username": op["username"],
        "status": "active",
        "note": "Agents remain departed. Reinstate individually.",
    }


# ── Rotate key ───────────────────────────────────────────────────────────────

@router.post("/{operator_id}/rotate-key", dependencies=[Depends(_require_admin)])
async def rotate_key(operator_id: UUID):
    """Deactivate all existing keys and issue a new one."""
    async with get_conn() as conn:
        op = await conn.fetchrow(
            "SELECT id, username FROM operators WHERE id = $1",
            operator_id,
        )
        if not op:
            raise HTTPException(status_code=404, detail="Operator not found")

        # Deactivate all existing keys
        await conn.execute(
            "UPDATE api_keys SET is_active = false WHERE operator_id = $1",
            operator_id,
        )

        # Create new key
        plaintext_key = await create_operator_key(conn, operator_id, "rotated")

    return {
        "uuid": str(op["id"]),
        "username": op["username"],
        "api_key": plaintext_key,
    }


# ── Set sharing scope ───────────────────────────────────────────────────────

_VALID_SCOPES = ("none", "intra", "full")


class SharingScopeRequest(BaseModel):
    sharing_scope: str


@router.patch("/{operator_id}/sharing-scope", dependencies=[Depends(_require_admin)])
async def set_sharing_scope(operator_id: UUID, body: SharingScopeRequest):
    """Set the sharing scope for an operator."""
    if body.sharing_scope not in _VALID_SCOPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid sharing_scope '{body.sharing_scope}'. Must be one of: {', '.join(_VALID_SCOPES)}",
        )

    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            UPDATE operators SET sharing_scope = $1, updated_at = now()
            WHERE id = $2
            RETURNING id, username, org, name, sharing_scope
            """,
            body.sharing_scope,
            operator_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Operator not found")

    return {
        "uuid": str(row["id"]),
        "username": row["username"],
        "org": row["org"],
        "display_name": row["name"],
        "sharing_scope": row["sharing_scope"],
    }
