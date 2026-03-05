from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_agent
from ..database import get_conn
from ..models import RetrieveRequest, RetrieveResponse, SkillExport, ViewCreate, ViewResponse
from ..services import view_service

router = APIRouter(tags=["views"])


@router.post("/agents/{agent_id}/views", response_model=ViewResponse, status_code=201)
async def create_view(agent_id: UUID, body: ViewCreate, agent=Depends(get_current_agent)):
    """Create a snapshot view — freezes matching atom IDs at this moment."""
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        result = await view_service.create_snapshot(
            conn=conn,
            owner_agent_id=agent_id,
            name=body.name,
            description=body.description,
            atom_filter=body.atom_filter,
        )
    return result


@router.get("/agents/{agent_id}/views", response_model=list[ViewResponse])
async def list_views(agent_id: UUID, agent=Depends(get_current_agent)):
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        results = await view_service.list_views(conn, agent_id)
    return results


@router.get("/agents/{agent_id}/views/{view_id}/export_skill", response_model=SkillExport)
async def export_skill(agent_id: UUID, view_id: UUID, agent=Depends(get_current_agent)):
    """Export a snapshot view as an α=1 skill package with rendered markdown."""
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        await _require_view_owner(conn, agent_id, view_id)
        result = await view_service.export_skill(conn, agent_id, view_id)
    if result is None:
        raise HTTPException(status_code=404, detail="View not found")
    return result


@router.post(
    "/agents/{agent_id}/shared_views/{view_id}/recall",
    response_model=RetrieveResponse,
)
async def recall_shared(
    agent_id: UUID, view_id: UUID, body: RetrieveRequest, agent=Depends(get_current_agent)
):
    """
    Recall through a shared view. Requires a valid, non-revoked capability.
    Graph expansion is scope-bounded to the snapshot's atoms only.
    """
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        cap = await _require_capability(conn, agent_id, view_id)
        result = await view_service.recall_shared(
            conn=conn,
            grantee_id=agent_id,
            view_id=view_id,
            capability_id=cap["id"],
            query=body.query,
            min_confidence=body.min_confidence,
            max_results=body.max_results,
            expansion_depth=body.expansion_depth,
        )
    return result


@router.get("/agents/{agent_id}/shared_views", response_model=list[ViewResponse])
async def list_shared_views(agent_id: UUID, agent=Depends(get_current_agent)):
    """List all views shared with this agent via active capabilities."""
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        rows = await conn.fetch(
            """
            SELECT v.id, v.owner_agent_id, v.name, v.description, v.alpha,
                   v.atom_filter, v.created_at,
                   COUNT(sa.atom_id) AS atom_count
            FROM capabilities c
            JOIN views v ON v.id = c.view_id
            LEFT JOIN snapshot_atoms sa ON sa.view_id = v.id
            WHERE c.grantee_id = $1
              AND c.revoked = false
              AND (c.expires_at IS NULL OR c.expires_at > now())
            GROUP BY v.id
            ORDER BY v.created_at DESC
            """,
            agent_id,
        )
    return [view_service._view_row(r) for r in rows]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_agent_access(agent: dict, agent_id: UUID):
    if agent["id"] and str(agent["id"]) != str(agent_id):
        raise HTTPException(status_code=403, detail="Forbidden")


async def _require_active_agent(conn, agent_id: UUID):
    row = await conn.fetchrow(
        "SELECT is_active FROM agents WHERE id = $1", agent_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not row["is_active"]:
        raise HTTPException(status_code=410, detail="Agent has departed")


async def _require_view_owner(conn, agent_id: UUID, view_id: UUID):
    row = await conn.fetchrow(
        "SELECT owner_agent_id FROM views WHERE id = $1", view_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="View not found")
    if row["owner_agent_id"] != agent_id:
        raise HTTPException(status_code=403, detail="Not view owner")


async def _require_capability(conn, grantee_id: UUID, view_id: UUID) -> dict:
    row = await conn.fetchrow(
        """
        SELECT id FROM capabilities
        WHERE grantee_id = $1
          AND view_id = $2
          AND revoked = false
          AND (expires_at IS NULL OR expires_at > now())
        LIMIT 1
        """,
        grantee_id,
        view_id,
    )
    if not row:
        raise HTTPException(status_code=403, detail="No valid capability for this view")
    return dict(row)
