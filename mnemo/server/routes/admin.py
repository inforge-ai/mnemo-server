"""
Admin router — protected by X-Admin-Token header.

Set MNEMO_ADMIN_TOKEN to enable. Requests without a matching token get 403.

Endpoints:
  GET /v1/admin/agents      — all agents with atom/key counts
  GET /v1/admin/operations  — operation counts by type (optional ?agent_id=)
  GET /v1/admin/keys        — all API keys with status
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import APIKeyHeader

from ..config import settings
from ..database import get_conn

router = APIRouter(tags=["admin"], prefix="/admin")

_token_header = APIKeyHeader(name="X-Admin-Token", auto_error=False)


def _require_admin(
    header_token: str | None = Depends(_token_header),
    token: str | None = Query(None),
):
    candidate = header_token or token
    if not settings.admin_token or candidate != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid or missing admin token")


# ── Agents ─────────────────────────────────────────────────────────────────────

@router.get("/agents", dependencies=[Depends(_require_admin)])
async def list_agents():
    """All agents with atom count and active key count."""
    async with get_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT
                a.id,
                a.name,
                a.persona,
                a.domain_tags,
                a.is_active,
                a.created_at,
                a.departed_at,
                COUNT(DISTINCT at.id) FILTER (WHERE at.is_active)  AS active_atoms,
                COUNT(DISTINCT at.id)                               AS total_atoms,
                COUNT(DISTINCT k.id)  FILTER (WHERE k.is_active)   AS active_keys
            FROM agents a
            LEFT JOIN atoms at ON at.agent_id = a.id
            LEFT JOIN api_keys k ON k.agent_id = a.id
            GROUP BY a.id
            ORDER BY a.created_at DESC
            """
        )
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "persona": r["persona"],
            "domain_tags": list(r["domain_tags"]) if r["domain_tags"] else [],
            "is_active": r["is_active"],
            "created_at": r["created_at"],
            "departed_at": r["departed_at"],
            "active_atoms": r["active_atoms"],
            "total_atoms": r["total_atoms"],
            "active_keys": r["active_keys"],
        }
        for r in rows
    ]


# ── Operations ─────────────────────────────────────────────────────────────────

@router.get("/operations", dependencies=[Depends(_require_admin)])
async def operation_counts(target_id: str | None = Query(None)):
    """Operation counts grouped by type. Filter by target_id (the agent whose memory was accessed)."""
    target_uuid: UUID | None = None
    if target_id is not None:
        try:
            target_uuid = UUID(target_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid target_id format")

    async with get_conn() as conn:
        if target_uuid is not None:
            rows = await conn.fetch(
                """
                SELECT operation,
                       COUNT(*)                        AS total,
                       AVG(duration_ms)::INT           AS avg_duration_ms,
                       MAX(created_at)                 AS last_at
                FROM operations
                WHERE target_id = $1
                GROUP BY operation
                ORDER BY total DESC
                """,
                target_uuid,
            )
            total_row = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM operations WHERE target_id = $1",
                target_uuid,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT operation,
                       COUNT(*)               AS total,
                       AVG(duration_ms)::INT  AS avg_duration_ms,
                       MAX(created_at)        AS last_at
                FROM operations
                GROUP BY operation
                ORDER BY total DESC
                """
            )
            total_row = await conn.fetchrow("SELECT COUNT(*) AS n FROM operations")

    return {
        "total": total_row["n"],
        "by_operation": [
            {
                "operation": r["operation"],
                "total": r["total"],
                "avg_duration_ms": r["avg_duration_ms"],
                "last_at": r["last_at"],
            }
            for r in rows
        ],
    }


# ── Glance ─────────────────────────────────────────────────────────────────────

@router.get("/glance", dependencies=[Depends(_require_admin)])
async def glance():
    """Glance custom-api format: agent/atom counts and today's operation totals."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    async with get_conn() as conn:
        agents_row = await conn.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE is_active)  AS active_agents,
                   COUNT(*)                            AS total_agents
            FROM agents
            """
        )
        atoms_row = await conn.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE is_active) AS active_atoms FROM atoms"
        )
        ops_rows = await conn.fetch(
            """
            SELECT operation, COUNT(*) AS n
            FROM operations
            WHERE created_at >= $1
            GROUP BY operation
            """,
            today,
        )

    ops_today = {r["operation"]: r["n"] for r in ops_rows}
    total_ops_today = sum(ops_today.values())

    return {
        "items": [
            {"title": "Agents",           "value": f"{agents_row['active_agents']} active"},
            {"title": "Atoms",            "value": f"{atoms_row['active_atoms']} total"},
            {"title": "Ops today",        "value": str(total_ops_today)},
            {"title": "Recalls today",    "value": str(ops_today.get("recall", 0))},
            {"title": "Remembers today",  "value": str(ops_today.get("remember", 0))},
        ]
    }


# ── Keys ───────────────────────────────────────────────────────────────────────

@router.get("/keys", dependencies=[Depends(_require_admin)])
async def key_status():
    """All API keys with agent name, prefix, status, and last use."""
    async with get_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT k.id,
                   k.key_prefix,
                   k.name        AS key_name,
                   k.is_active,
                   k.created_at,
                   k.last_used_at,
                   a.id          AS agent_id,
                   a.name        AS agent_name
            FROM api_keys k
            JOIN agents a ON a.id = k.agent_id
            ORDER BY k.created_at DESC
            """
        )
    return [
        {
            "id": str(r["id"]),
            "key_prefix": r["key_prefix"],
            "key_name": r["key_name"],
            "is_active": r["is_active"],
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"],
            "agent_id": str(r["agent_id"]),
            "agent_name": r["agent_name"],
        }
        for r in rows
    ]
