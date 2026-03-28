"""
Admin router — protected by X-Admin-Key header (RBAC-Lite).

Endpoints:
  GET /v1/admin/operations  — operation counts by type (optional ?agent_id=)
  GET /v1/admin/keys        — all API keys with status
  GET /v1/admin/glance      — system overview dashboard

Agent endpoints moved to admin_agents.py.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import require_admin as _require_admin  # noqa: F401 — re-exported for other admin routes
from ..database import get_conn

router = APIRouter(tags=["admin"], prefix="/admin")


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
            SELECT COUNT(*) FILTER (WHERE status = 'active')  AS active_agents,
                   COUNT(*)                                    AS total_agents
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
                   o.id          AS operator_id,
                   o.name        AS operator_name
            FROM api_keys k
            JOIN operators o ON o.id = k.operator_id
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
            "operator_id": str(r["operator_id"]),
            "operator_name": r["operator_name"],
        }
        for r in rows
    ]
