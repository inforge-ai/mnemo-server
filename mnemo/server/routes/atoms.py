from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_operator, verify_agent_ownership
from ..database import get_conn, get_pool
from ..services.address_service import resolve_agent_identifier
from ..models import AtomCreate, AtomResponse, EdgeCreate, EdgeResponse
from ..services import atom_service

router = APIRouter(tags=["atoms"])


@router.post("/agents/{agent_id}/atoms", response_model=AtomResponse, status_code=201)
async def create_atom(agent_id: str, body: AtomCreate, operator=Depends(get_current_operator)):
    """Explicit typed atom creation (power-user interface)."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        result = await atom_service.store_explicit(
            conn=conn,
            agent_id=agent_uuid,
            atom_type=body.atom_type,
            text_content=body.text_content,
            structured=body.structured,
            confidence_label=body.confidence,
            source_type=body.source_type,
            source_ref=body.source_ref,
            domain_tags=body.domain_tags,
        )
    return result


@router.get("/agents/{agent_id}/atoms/{atom_id}", response_model=AtomResponse)
async def get_atom(agent_id: str, atom_id: UUID, operator=Depends(get_current_operator)):
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        result = await atom_service.get_atom(conn, agent_uuid, atom_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Atom not found")
    return result


@router.delete("/agents/{agent_id}/atoms/{atom_id}", status_code=204)
async def delete_atom(agent_id: str, atom_id: UUID, operator=Depends(get_current_operator)):
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        deleted = await atom_service.soft_delete_atom(conn, agent_uuid, atom_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Atom not found or already inactive")


@router.post("/agents/{agent_id}/atoms/link", response_model=EdgeResponse, status_code=201)
async def link_atoms(agent_id: str, body: EdgeCreate, operator=Depends(get_current_operator)):
    """Create an edge between two atoms (both must belong to this agent)."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)

        # Verify both atoms belong to this agent
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM atoms
            WHERE id = ANY($1) AND agent_id = $2 AND is_active = true
            """,
            [body.source_id, body.target_id],
            agent_uuid,
        )
        if count < 2:
            raise HTTPException(
                status_code=404,
                detail="One or both atoms not found or do not belong to this agent",
            )

        result = await atom_service.create_edge(
            conn=conn,
            source_id=body.source_id,
            target_id=body.target_id,
            edge_type=body.edge_type,
            weight=body.weight,
        )
    if result is None:
        raise HTTPException(status_code=409, detail="Edge already exists")
    return result


async def _require_active_agent(conn, agent_id: UUID):
    row = await conn.fetchrow(
        "SELECT is_active FROM agents WHERE id = $1",
        agent_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not row["is_active"]:
        raise HTTPException(status_code=410, detail="Agent has departed")
