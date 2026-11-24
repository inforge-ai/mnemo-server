"""v2 Phase 2 relation API (docs/phase2-design.md §7): supersede, undo, conflicts.

Agent key only; the owning agent only.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import AuthContext, require_agent, require_agent_match
from ..database import get_conn, get_pool
from ..models import (
    ConflictListResponse, SupersedeRequest, SupersedeResponse, UndoRequest, UndoResponse,
)
from ..services import relation_service, store_service
from ..services.address_service import resolve_agent_identifier
from ..services.ops_service import log_operation

router = APIRouter(tags=["relations"])


async def _owning_agent(agent_id: str, auth: AuthContext):
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id, operator_id=auth.operator_id)
    require_agent_match(agent_uuid, auth)
    async with get_conn() as conn:
        agent = await conn.fetchrow("SELECT status, name, operator_id, metadata FROM agents WHERE id = $1", agent_uuid)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent["status"] != "active":
        raise HTTPException(status_code=410, detail="Agent has departed")
    return pool, agent_uuid, agent


@router.post("/agents/{agent_id}/atoms/{atom_id}/supersede", response_model=SupersedeResponse, status_code=201)
async def supersede(agent_id: str, atom_id: UUID, body: SupersedeRequest, auth: AuthContext = Depends(require_agent)):
    """Mark `atom_id` as replaced, by an existing atom (`by_atom_id`) or by a new
    memory (`text`, stored through remember with this atom as its forced target)."""
    if (body.by_atom_id is None) == (body.text is None):
        raise HTTPException(status_code=422, detail="give exactly one of by_atom_id or text")
    pool, agent_uuid, agent = await _owning_agent(agent_id, auth)
    store_id = None
    by_atom_id = body.by_atom_id
    if body.text is not None:
        async with get_conn() as conn:
            exists = await conn.fetchval("SELECT status FROM atoms WHERE id = $1 AND agent_id = $2", atom_id, agent_uuid)
            if exists is None:
                raise HTTPException(status_code=404, detail="atom not found")
            if exists != "active":
                raise HTTPException(status_code=409, detail=f"atom is {exists}; only an active atom can be superseded")
            src = await store_service.create_store(
                conn, agent_id=agent_uuid, operator_id=agent["operator_id"], agent_name=agent["name"],
                agent_metadata=agent["metadata"], text=body.text, domain_tags=[], remembered_on=None,
                speaker=None, addressee=None, channel=None, origin_class="unspecified",
                exclude_candidates=[atom_id])
        outcome = await store_service.run_store(pool, src)
        if outcome["status"] != "complete":
            raise HTTPException(status_code=outcome["http_status"], detail=outcome.get("error") or "store failed")
        store_id = src.store_id
        new_ids = [a["id"] for a in outcome["atoms"] if not a.get("merged_into_existing")]
        if not new_ids:
            raise HTTPException(status_code=409, detail="the text produced no new atom")
        async with get_conn() as conn:
            by_atom_id = await conn.fetchval(
                "SELECT n.id FROM atoms n, atoms o WHERE n.id = ANY($1) AND o.id = $2 "
                "ORDER BY n.embedding <=> o.embedding LIMIT 1", new_ids, atom_id)
    async with get_conn() as conn:
        async with conn.transaction():
            try:
                out = await relation_service.supersede(conn, agent_uuid, atom_id, by_atom_id, body.reason)
            except relation_service.RelationApiError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail)
        await log_operation(conn, "supersede", auth.agent_id, target_id=atom_id)
    return {**out, "store_id": store_id}


@router.post("/edges/{edge_id}/undo", response_model=UndoResponse)
async def undo(edge_id: UUID, body: UndoRequest | None = None, auth: AuthContext = Depends(require_agent)):
    """Reverse a `replaces` or `same`; the edge is retired, never deleted."""
    async with get_conn() as conn:
        async with conn.transaction():
            try:
                out = await relation_service.undo(conn, auth.agent_id, edge_id)
            except relation_service.RelationApiError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail)
        await log_operation(conn, "undo_relation", auth.agent_id, target_id=out["restored_atom_id"])
    return out


@router.get("/agents/{agent_id}/conflicts", response_model=ConflictListResponse)
async def conflicts(agent_id: str, open: bool = True, limit: int = Query(20, ge=1, le=100),
                    auth: AuthContext = Depends(require_agent)):
    """Conflicts, ranked by the lower confidence of the two sides, then whether
    their origin classes differ, then age."""
    _, agent_uuid, _ = await _owning_agent(agent_id, auth)
    async with get_conn() as conn:
        rows = await relation_service.list_conflicts(conn, agent_uuid, open_only=open, limit=limit)
    return {"conflicts": rows}
