from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_agent
from ..database import get_conn
from ..models import AtomCreate, AtomResponse, EdgeCreate, EdgeResponse
from ..services import atom_service

router = APIRouter(tags=["atoms"])


@router.post("/agents/{agent_id}/atoms", response_model=AtomResponse, status_code=201)
async def create_atom(agent_id: UUID, body: AtomCreate, agent=Depends(get_current_agent)):
    """Explicit typed atom creation (power-user interface)."""
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        result = await atom_service.store_explicit(
            conn=conn,
            agent_id=agent_id,
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
async def get_atom(agent_id: UUID, atom_id: UUID, agent=Depends(get_current_agent)):
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        result = await atom_service.get_atom(conn, agent_id, atom_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Atom not found")
    return result


@router.delete("/agents/{agent_id}/atoms/{atom_id}", status_code=204)
async def delete_atom(agent_id: UUID, atom_id: UUID, agent=Depends(get_current_agent)):
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        deleted = await atom_service.soft_delete_atom(conn, agent_id, atom_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Atom not found or already inactive")


@router.post("/agents/{agent_id}/atoms/link", response_model=EdgeResponse, status_code=201)
async def link_atoms(agent_id: UUID, body: EdgeCreate, agent=Depends(get_current_agent)):
    """Create an edge between two atoms (both must belong to this agent)."""
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)

        # Verify both atoms belong to this agent
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM atoms
            WHERE id = ANY($1) AND agent_id = $2 AND is_active = true
            """,
            [body.source_id, body.target_id],
            agent_id,
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


def _check_agent_access(agent: dict, agent_id: UUID):
    if agent["id"] and str(agent["id"]) != str(agent_id):
        raise HTTPException(status_code=403, detail="Forbidden")


async def _require_active_agent(conn, agent_id: UUID):
    row = await conn.fetchrow(
        "SELECT is_active FROM agents WHERE id = $1",
        agent_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not row["is_active"]:
        raise HTTPException(status_code=410, detail="Agent has departed")
