import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_operator, verify_agent_ownership
from ..database import get_conn, get_pool
from ..services.address_service import resolve_agent_identifier
from ..models import RememberRequest, RememberResponse, RetrieveRequest, RetrieveResponse
from ..services import atom_service
from ..services.ops_service import log_operation

router = APIRouter(tags=["memory"])


@router.post("/agents/{agent_id}/remember", response_model=RememberResponse, status_code=201)
async def remember(agent_id: str, body: RememberRequest, operator=Depends(get_current_operator)):
    """Store a free-text memory. Server decomposes, deduplicates, and links atoms."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        t0 = time.monotonic()
        result = await atom_service.store_from_text(
            conn=conn,
            agent_id=agent_uuid,
            text=body.text,
            domain_tags=body.domain_tags,
        )
        await log_operation(
            conn, "remember", operator["id"], target_id=agent_uuid,
            duration_ms=int((time.monotonic() - t0) * 1000),
            metadata={"atoms_created": result["atoms_created"]},
        )
    return result


@router.post("/agents/{agent_id}/recall", response_model=RetrieveResponse)
async def recall(agent_id: str, body: RetrieveRequest, operator=Depends(get_current_operator)):
    """Retrieve relevant memories via semantic search + optional graph expansion."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        t0 = time.monotonic()
        result = await atom_service.retrieve(
            conn=conn,
            agent_id=agent_uuid,
            query=body.query,
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
            conn, "recall", operator["id"], target_id=agent_uuid,
            duration_ms=int((time.monotonic() - t0) * 1000),
            metadata={"results_returned": result["total_retrieved"]},
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
