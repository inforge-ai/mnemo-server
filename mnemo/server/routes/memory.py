import logging
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from ..auth import AuthContext, require_agent, require_agent_match, resolve_auth
from ..config import settings
from ..database import get_conn, get_pool
from ..services.address_service import resolve_agent_identifier
from ..models import (
    RecentRequest, RecentResponse, RememberRequest, RememberResponse,
    RetrieveRequest, RetrieveResponse, StoreJobResponse,
)
from ..services import atom_service, store_service
from ..services.ops_service import log_operation

logger = logging.getLogger(__name__)

router = APIRouter(tags=["memory"])


def _store_outcome_response(outcome: dict) -> JSONResponse:
    """Map a store_service outcome to the HTTP response (201 / 502 / 500)."""
    if outcome["status"] == "complete":
        body = RememberResponse(**{k: v for k, v in outcome.items() if k != "http_status"})
        return JSONResponse(status_code=201, content=body.model_dump(mode="json"))
    return JSONResponse(
        status_code=outcome["http_status"],
        content={
            "status": "failed",
            "store_id": str(outcome["store_id"]),
            "source_id": str(outcome["source_id"]),
            "error": outcome["error"],
        },
    )


@router.post(
    "/agents/{agent_id}/remember",
    status_code=201,
    response_model=RememberResponse,
    responses={502: {"description": "Decomposer failed; the source text is kept and the store can be retried."}},
)
async def remember(agent_id: str, body: RememberRequest, auth: AuthContext = Depends(require_agent)):
    """Store a free-text memory.

    wait=true (default): returns when storage is complete, with the atoms
    stored. wait=false: returns immediately with status "queued".
    The source text is written before decomposition, so a failure loses
    nothing (POST /stores/{store_id}/retry).
    """
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
    agent_uuid = await resolve_agent_identifier(pool, agent_id, operator_id=auth.operator_id)
    require_agent_match(agent_uuid, auth)
    async with get_conn() as conn:
        agent = await conn.fetchrow(
            "SELECT status, name, operator_id, metadata FROM agents WHERE id = $1", agent_uuid,
        )
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        if agent["status"] != "active":
            raise HTTPException(status_code=410, detail="Agent has departed")
        await log_operation(conn, "remember", auth.agent_id, target_id=agent_uuid)
        src = await store_service.create_store(
            conn,
            agent_id=agent_uuid,
            operator_id=agent["operator_id"],
            agent_name=agent["name"],
            agent_metadata=agent["metadata"],
            text=body.text,
            domain_tags=body.domain_tags,
            remembered_on=body.remembered_on,
            speaker=body.speaker,
            addressee=body.addressee,
            channel=body.channel,
            origin_class=body.origin_class,
        )

    if body.wait:
        return _store_outcome_response(await store_service.run_store(pool, src))

    coro = store_service.run_store(pool, src)
    if settings.sync_store_for_tests:
        await coro
    else:
        store_service.spawn(coro)
    return JSONResponse(status_code=201, content={
        "status": "queued", "store_id": str(src.store_id), "source_id": str(src.source_id),
    })


@router.post("/agents/{agent_id}/recall", response_model=RetrieveResponse, response_model_exclude_none=True)
async def recall(agent_id: str, body: RetrieveRequest, auth: AuthContext = Depends(require_agent)):
    """Retrieve relevant memories via semantic search + optional graph expansion."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id, operator_id=auth.operator_id)
    require_agent_match(agent_uuid, auth)
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
            since=body.since,
            until=body.until,
            atom_types=body.atom_types,
        )
        await log_operation(
            conn, "recall", auth.agent_id, target_id=agent_uuid,
            duration_ms=int((time.monotonic() - t0) * 1000),
            metadata={"results_returned": result["total_retrieved"]},
        )
    return result


@router.post("/agents/{agent_id}/recent", response_model=RecentResponse, response_model_exclude_none=True)
async def recent(agent_id: str, body: RecentRequest, auth: AuthContext = Depends(require_agent)):
    """Newest atoms first (order_by "stored" = created_at, "event" =
    COALESCE(remembered_on, created_at)). No embedding, no access update."""
    pool = await get_pool()
    agent_uuid = await resolve_agent_identifier(pool, agent_id, operator_id=auth.operator_id)
    require_agent_match(agent_uuid, auth)
    async with get_conn() as conn:
        await _require_active_agent(conn, agent_uuid)
        t0 = time.monotonic()
        atoms = await atom_service.recent(
            conn, agent_uuid, limit=body.limit, since=body.since, until=body.until,
            atom_types=body.atom_types, domain_tags=body.domain_tags,
            include_superseded=body.include_superseded, order_by=body.order_by,
        )
        await log_operation(
            conn, "recent", auth.agent_id, target_id=agent_uuid,
            duration_ms=int((time.monotonic() - t0) * 1000),
            metadata={"results_returned": len(atoms)},
        )
    return {"atoms": atoms, "total_returned": len(atoms)}


@router.get("/stores/{store_id}/status", response_model=StoreJobResponse)
async def store_status(store_id: UUID, auth: AuthContext = Depends(resolve_auth)):
    """Status of a store, with its atoms. Visible to the owning agent, the
    owning operator, or admin; anyone else gets 404."""
    async with get_conn() as conn:
        row = await store_service.job_visible_to(conn, store_id, auth)
        if not row:
            raise HTTPException(status_code=404, detail="Store job not found")
        atoms = await store_service.store_atoms(conn, row["source_id"])
    return {
        "store_id": row["store_id"], "status": row["status"], "atoms_created": row["atoms_created"],
        "created_at": row["created_at"], "completed_at": row["completed_at"], "error": row["error"],
        "source_id": row["source_id"], "atoms": atoms,
    }


@router.post(
    "/stores/{store_id}/retry",
    status_code=201,
    response_model=RememberResponse,
    responses={409: {"description": "Store is not in the failed state."}},
)
async def store_retry(store_id: UUID, auth: AuthContext = Depends(resolve_auth)):
    """Re-run a failed store from its source row (same store_id/source_id).
    Safe to repeat: a failed store wrote no atoms."""
    pool = await get_pool()
    async with get_conn() as conn:
        row = await store_service.job_visible_to(conn, store_id, auth)
        if not row:
            raise HTTPException(status_code=404, detail="Store job not found")
        if row["status"] != "failed":
            raise HTTPException(status_code=409, detail=f"Store is {row['status']}; only failed stores can be retried")
        src = await store_service.load_store(conn, store_id)
        if src is None:
            raise HTTPException(status_code=409, detail="Store has no source row (stored before v2 Phase 1)")
        await log_operation(conn, "store_retry", auth.agent_id, target_id=src.agent_id)
    return _store_outcome_response(await store_service.run_store(pool, src))


async def _require_active_agent(conn, agent_id: UUID):
    row = await conn.fetchrow(
        "SELECT status FROM agents WHERE id = $1",
        agent_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    if row["status"] != "active":
        raise HTTPException(status_code=410, detail="Agent has departed")
