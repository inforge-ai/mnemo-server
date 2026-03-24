import asyncio
import logging
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_operator, verify_agent_ownership
from ..config import settings
from ..database import get_conn, get_pool
from ..services.address_service import resolve_agent_identifier
from ..models import RememberRequest, RememberResponse, RetrieveRequest, RetrieveResponse
from ..services import atom_service
from ..services.ops_service import log_operation

logger = logging.getLogger(__name__)

router = APIRouter(tags=["memory"])


@router.post("/agents/{agent_id}/remember", response_model=RememberResponse, status_code=201)
async def remember(agent_id: str, body: RememberRequest, operator=Depends(get_current_operator)):
    """Store a free-text memory. Returns immediately; decomposition runs in background."""
    # ── Input validation ──
    stripped = body.text.strip()
    if not stripped:
        raise HTTPException(status_code=422, detail="text must contain non-whitespace content")
    if len(stripped) < 3:
        raise HTTPException(status_code=422, detail="text must be at least 3 characters")
    if len(body.text) > 50_000:
        raise HTTPException(status_code=413, detail=(
            "text exceeds maximum length of 50,000 characters. "
            "Split large documents into smaller sections before storing."
        ))
    if len(body.text) > 10_000:
        logger.warning("Large input: %d chars from agent %s", len(body.text), agent_id)

    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id)
    await verify_agent_ownership(operator, agent_uuid)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        # Resolve operator_id for decomposer usage logging
        op_row = await conn.fetchrow(
            "SELECT operator_id FROM agents WHERE id = $1", agent_uuid,
        )
        operator_id = op_row["operator_id"] if op_row else None

    store_id = uuid4()
    async with get_conn() as conn:
        await log_operation(conn, "remember", operator["id"], target_id=agent_uuid)
    coro = atom_service.store_background(
        pool=pool,
        store_id=store_id,
        agent_id=agent_uuid,
        text=body.text,
        domain_tags=body.domain_tags,
        operator_id=operator_id,
        remembered_on=body.remembered_on,
    )
    if settings.sync_store_for_tests:
        await coro
    else:
        asyncio.create_task(coro)
    return {"status": "queued", "store_id": store_id}


@router.post("/agents/{agent_id}/recall", response_model=RetrieveResponse, response_model_exclude_none=True)
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
