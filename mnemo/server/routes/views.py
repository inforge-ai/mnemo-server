import json
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_operator, verify_agent_ownership
from ..database import get_conn, get_pool
from ..services.address_service import resolve_agent_identifier
from ..models import RetrieveRequest, RetrieveResponse, SharedRecallRequest, SharedViewResponse, SkillExport, ViewCreate, ViewResponse
from ..services import view_service
from ..services.ops_service import log_operation
from ..services.platform_service import is_sharing_enabled

router = APIRouter(tags=["views"])


@router.post("/agents/{agent_id}/views", response_model=ViewResponse, status_code=201)
async def create_view(agent_id: str, body: ViewCreate, operator=Depends(get_current_operator)):
    """Create a snapshot view — freezes matching atom IDs at this moment."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        result = await view_service.create_snapshot(
            conn=conn,
            owner_agent_id=agent_uuid,
            name=body.name,
            description=body.description,
            atom_filter=body.atom_filter,
        )
    return result


@router.get("/agents/{agent_id}/views", response_model=list[ViewResponse])
async def list_views(agent_id: str, operator=Depends(get_current_operator)):
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        results = await view_service.list_views(conn, agent_uuid)
    return results


@router.post("/agents/{agent_id}/shared_views/recall")
async def recall_all_shared_endpoint(agent_id: str, body: SharedRecallRequest, operator=Depends(get_current_operator)):
    """Search across all views shared with this agent."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)

    from_agent_id = None
    if body.from_agent:
        from ..services.address_service import resolve_address
        from_agent_id = await resolve_address(pool, body.from_agent)

    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)

        # Global sharing toggle
        if not await is_sharing_enabled(conn):
            return {"atoms": [], "note": "Sharing is currently disabled"}

        result = await view_service.recall_all_shared(
            conn=conn,
            grantee_id=agent_uuid,
            query=body.query,
            from_agent_id=from_agent_id,
            min_similarity=body.min_similarity,
            max_results=body.max_results,
        )
    return result


@router.get("/agents/{agent_id}/views/{view_id}/export_skill", response_model=SkillExport)
async def export_skill(agent_id: str, view_id: UUID, operator=Depends(get_current_operator)):
    """Export a snapshot view as an α=1 skill package with rendered markdown."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        await _require_view_owner(conn, agent_uuid, view_id)
        t0 = time.monotonic()
        result = await view_service.export_skill(conn, agent_uuid, view_id)
        if result is not None:
            await log_operation(
                conn, "export_skill", operator["id"], target_id=agent_uuid,
                duration_ms=int((time.monotonic() - t0) * 1000),
                metadata={"view_id": str(view_id)},
            )
    if result is None:
        raise HTTPException(status_code=404, detail="View not found")
    return result


@router.post(
    "/agents/{agent_id}/shared_views/{view_id}/recall",
    response_model=RetrieveResponse,
)
async def recall_shared(
    agent_id: str, view_id: UUID, body: RetrieveRequest, operator=Depends(get_current_operator)
):
    """
    Recall through a shared view. Requires a valid, non-revoked capability.
    Graph expansion is scope-bounded to the snapshot's atoms only.
    """
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)

        # Global sharing toggle
        if not await is_sharing_enabled(conn):
            raise HTTPException(status_code=403, detail="Sharing is currently disabled")

        cap = await _require_capability(conn, agent_uuid, view_id)
        t0 = time.monotonic()
        result = await view_service.recall_shared(
            conn=conn,
            grantee_id=agent_uuid,
            view_id=view_id,
            capability_id=cap["id"],
            query=body.query,
            min_confidence=body.min_confidence,
            max_results=body.max_results,
            expansion_depth=body.expansion_depth,
        )
        await log_operation(
            conn, "recall_shared", operator["id"], target_id=agent_uuid,
            duration_ms=int((time.monotonic() - t0) * 1000),
            metadata={"view_id": str(view_id), "results_returned": result["total_retrieved"]},
        )
    return result


@router.get("/agents/{agent_id}/shared_views", response_model=list[SharedViewResponse])
async def list_shared_views(agent_id: str, operator=Depends(get_current_operator)):
    """List all views shared with this agent via active capabilities."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        rows = await conn.fetch(
            """
            SELECT v.id, v.owner_agent_id, v.name, v.description, v.alpha,
                   v.atom_filter, v.created_at,
                   COUNT(sa.atom_id) AS atom_count,
                   c.grantor_id,
                   c.created_at AS granted_at,
                   aa.address AS source_address,
                   bool_or(at.id IS NOT NULL) AS trusted
            FROM capabilities c
            JOIN views v ON v.id = c.view_id
            LEFT JOIN snapshot_atoms sa ON sa.view_id = v.id
            LEFT JOIN agent_addresses aa ON aa.agent_id = c.grantor_id
            LEFT JOIN agent_trust at ON at.agent_uuid = c.grantee_id AND at.trusted_sender_uuid = c.grantor_id
            WHERE c.grantee_id = $1
              AND c.revoked = false
              AND (c.expires_at IS NULL OR c.expires_at > now())
            GROUP BY v.id, c.grantor_id, c.created_at, aa.address
            ORDER BY v.created_at DESC
            """,
            agent_uuid,
        )
    return [_shared_view_row(r) for r in rows]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _shared_view_row(row) -> dict:
    af = row["atom_filter"]
    if isinstance(af, str):
        af = json.loads(af)
    return {
        "id": row["id"],
        "owner_agent_id": row["owner_agent_id"],
        "name": row["name"],
        "description": row["description"],
        "alpha": row["alpha"],
        "atom_filter": af or {},
        "atom_count": row["atom_count"],
        "created_at": row["created_at"],
        "grantor_id": row["grantor_id"],
        "source_address": row["source_address"],
        "granted_at": row["granted_at"],
        "trusted": row["trusted"],
    }

async def _require_active_agent(conn, agent_id: UUID):
    row = await conn.fetchrow(
        "SELECT status FROM agents WHERE id = $1", agent_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    if row["status"] != "active":
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
