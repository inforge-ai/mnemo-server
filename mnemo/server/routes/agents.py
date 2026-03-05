import json
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import get_current_agent
from ..database import get_conn
from ..models import AgentCreate, AgentResponse, AgentStats
from ..services.atom_service import get_agent_stats

router = APIRouter(tags=["agents"])


@router.post("/agents", response_model=AgentResponse, status_code=201)
async def register_agent(body: AgentCreate):
    try:
        async with get_conn() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agents (name, persona, domain_tags, metadata)
                VALUES ($1, $2, $3, $4)
                RETURNING id, name, persona, domain_tags, metadata, created_at, is_active
                """,
                body.name,
                body.persona,
                body.domain_tags,
                json.dumps(body.metadata),
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail=f"Agent name '{body.name}' already exists")
    return _agent_row(row)


@router.get("/agents", response_model=list[AgentResponse])
async def find_agents(name: str = Query(..., description="Exact agent name to look up")):
    """Find active agents by exact name. Returns empty list if none found."""
    async with get_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, persona, domain_tags, metadata, created_at, is_active
            FROM agents WHERE name = $1 AND is_active = true
            ORDER BY created_at ASC
            """,
            name,
        )
    return [_agent_row(r) for r in rows]


@router.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: UUID):
    async with get_conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, persona, domain_tags, metadata, created_at, is_active
            FROM agents WHERE id = $1
            """,
            agent_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _agent_row(row)


@router.get("/agents/{agent_id}/stats", response_model=AgentStats)
async def agent_stats(agent_id: UUID, agent=Depends(get_current_agent)):
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        stats = await get_agent_stats(conn, agent_id)
    return stats


@router.post("/agents/{agent_id}/depart")
async def depart_agent(agent_id: UUID, agent=Depends(get_current_agent)):
    """
    Agent departure:
    1. Cascade-revoke all capabilities this agent granted.
    2. Mark agent inactive with departure + expiry timestamps.
    3. Return summary.
    """
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        row = await conn.fetchrow(
            "SELECT id, is_active FROM agents WHERE id = $1",
            agent_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Agent not found")
        if not row["is_active"]:
            raise HTTPException(status_code=409, detail="Agent already departed")

        # Cascade-revoke capabilities
        revoked_count = await conn.fetchval(
            "SELECT revoke_agent_capabilities($1)",
            agent_id,
        )

        # Mark departed
        row = await conn.fetchrow(
            """
            UPDATE agents
            SET is_active       = false,
                departed_at     = now(),
                data_expires_at = now() + interval '30 days'
            WHERE id = $1
            RETURNING departed_at, data_expires_at
            """,
            agent_id,
        )

        # Audit log
        await conn.execute(
            """
            INSERT INTO access_log (agent_id, action, metadata)
            VALUES ($1, 'departure', $2)
            """,
            agent_id,
            json.dumps({"capabilities_revoked": revoked_count}),
        )

    return {
        "capabilities_revoked": revoked_count,
        "departed_at": row["departed_at"],
        "data_expires_at": row["data_expires_at"],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_agent_access(agent: dict, agent_id: UUID):
    """Raise 403 if auth is active and the authenticated agent doesn't match path agent_id."""
    if agent["id"] and str(agent["id"]) != str(agent_id):
        raise HTTPException(status_code=403, detail="Forbidden")


def _agent_row(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "persona": row["persona"],
        "domain_tags": list(row["domain_tags"]) if row["domain_tags"] else [],
        "metadata": json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {}),
        "created_at": row["created_at"],
        "is_active": row["is_active"],
    }


async def _require_active_agent(conn, agent_id: UUID):
    row = await conn.fetchrow(
        "SELECT is_active FROM agents WHERE id = $1",
        agent_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not row["is_active"]:
        raise HTTPException(status_code=410, detail="Agent has departed")
