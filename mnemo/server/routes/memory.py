from uuid import UUID

from fastapi import APIRouter, HTTPException

from ..database import get_conn
from ..models import RememberRequest, RememberResponse, RetrieveRequest, RetrieveResponse
from ..services import atom_service

router = APIRouter(tags=["memory"])


@router.post("/agents/{agent_id}/remember", response_model=RememberResponse, status_code=201)
async def remember(agent_id: UUID, body: RememberRequest):
    """Store a free-text memory. Server decomposes, deduplicates, and links atoms."""
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        result = await atom_service.store_from_text(
            conn=conn,
            agent_id=agent_id,
            text=body.text,
            domain_tags=body.domain_tags,
        )
    return result


@router.post("/agents/{agent_id}/recall", response_model=RetrieveResponse)
async def recall(agent_id: UUID, body: RetrieveRequest):
    """Retrieve relevant memories via semantic search + optional graph expansion."""
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        result = await atom_service.retrieve(
            conn=conn,
            agent_id=agent_id,
            query=body.query,
            atom_types=body.atom_types,
            domain_tags=body.domain_tags,
            min_confidence=body.min_confidence,
            min_similarity=body.min_similarity,
            max_results=body.max_results,
            expand_graph=body.expand_graph,
            expansion_depth=body.expansion_depth,
            include_superseded=body.include_superseded,
        )
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
