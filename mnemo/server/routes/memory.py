import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_agent
from ..database import get_conn
from ..models import RememberRequest, RememberResponse, RetrieveRequest, RetrieveResponse
from ..services import atom_service
from ..services.ops_service import log_operation

router = APIRouter(tags=["memory"])


@router.post("/agents/{agent_id}/remember", response_model=RememberResponse, status_code=201)
async def remember(agent_id: UUID, body: RememberRequest, agent=Depends(get_current_agent)):
    """Store a free-text memory. Server decomposes, deduplicates, and links atoms."""
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        t0 = time.monotonic()
        result = await atom_service.store_from_text(
            conn=conn,
            agent_id=agent_id,
            text=body.text,
            domain_tags=body.domain_tags,
        )
        await log_operation(
            conn, "remember", agent["id"], target_id=agent_id,
            duration_ms=int((time.monotonic() - t0) * 1000),
            metadata={"atoms_created": result["atoms_created"]},
        )
    return result


@router.post("/agents/{agent_id}/recall", response_model=RetrieveResponse)
async def recall(agent_id: UUID, body: RetrieveRequest, agent=Depends(get_current_agent)):
    """Retrieve relevant memories via semantic search + optional graph expansion."""
    _check_agent_access(agent, agent_id)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_id)
        t0 = time.monotonic()
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
            similarity_drop_threshold=body.similarity_drop_threshold,
            verbosity=body.verbosity,
            max_content_chars=body.max_content_chars,
            max_total_tokens=body.max_total_tokens,
        )
        await log_operation(
            conn, "recall", agent["id"], target_id=agent_id,
            duration_ms=int((time.monotonic() - t0) * 1000),
            metadata={"results_returned": result["total_retrieved"]},
        )
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
