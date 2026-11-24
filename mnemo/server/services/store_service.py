"""Store orchestration for /remember (docs/phase1-design.md §2–3).

    route ── create_store: tx1 store_jobs(pending) + sources row
          ── run_store: decompose + embed + check (no connection held)
                        tx2 all inserts + store_jobs(complete)
          └─ background lifecycle detection (tracked tasks)

A failed store keeps its source row and can be retried with the same ids.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

import asyncpg

from ..config import settings
from ..decomposers import DecomposeRequest, DecomposerError
from . import atom_service

logger = logging.getLogger(__name__)

# ── Background tasks: keep a reference to each until it finishes ────────────

_background: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def background_tasks() -> set[asyncio.Task]:
    return set(_background)


async def drain_background(timeout: float = 10.0) -> None:
    """Shutdown: wait for background tasks, then cancel what is left."""
    tasks = list(_background)
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:
        t.cancel()
    if pending:
        logger.warning("cancelled %d background store task(s) at shutdown", len(pending))


# ── Store records ───────────────────────────────────────────────────────────

@dataclass
class StoreSource:
    store_id: UUID
    source_id: UUID
    agent_id: UUID
    operator_id: UUID | None
    agent_name: str
    text: str
    speaker: str
    addressee: str | None = None
    channel: str | None = None
    origin_class: str = "unspecified"
    remembered_on: datetime | None = None
    domain_tags: list[str] = field(default_factory=list)
    user_name: str | None = None

    def request(self) -> DecomposeRequest:
        return DecomposeRequest(
            text=self.text, agent_name=self.agent_name, speaker=self.speaker,
            addressee=self.addressee, channel=self.channel,
            remembered_on=self.remembered_on, user_name=self.user_name,
        )


def _user_name(metadata) -> str | None:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return None
    value = (metadata or {}).get("user_name")
    return value.strip() if isinstance(value, str) and value.strip() else None


async def create_store(
    conn: asyncpg.Connection,
    *,
    agent_id: UUID,
    operator_id: UUID | None,
    agent_name: str,
    agent_metadata,
    text: str,
    domain_tags: list[str],
    remembered_on: datetime | None,
    speaker: str | None,
    addressee: str | None,
    channel: str | None,
    origin_class: str,
) -> StoreSource:
    """tx1: the job row and the source row, committed before decomposition."""
    src = StoreSource(
        store_id=uuid4(), source_id=uuid4(), agent_id=agent_id, operator_id=operator_id,
        agent_name=agent_name, text=text, speaker=speaker or agent_name,
        addressee=addressee, channel=channel, origin_class=origin_class,
        remembered_on=remembered_on, domain_tags=list(domain_tags or []),
        user_name=_user_name(agent_metadata),
    )
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO store_jobs (store_id, agent_id, operator_id) VALUES ($1, $2, $3)",
            src.store_id, agent_id, operator_id,
        )
        await conn.execute(
            """
            INSERT INTO sources (id, agent_id, store_id, text, speaker, addressee,
                                 channel, origin_class, remembered_on, domain_tags)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            src.source_id, agent_id, src.store_id, text, src.speaker, addressee,
            channel, origin_class, remembered_on, src.domain_tags,
        )
    return src


async def load_store(conn: asyncpg.Connection, store_id: UUID) -> StoreSource | None:
    row = await conn.fetchrow(
        """
        SELECT s.id AS source_id, s.store_id, s.agent_id, s.text, s.speaker, s.addressee,
               s.channel, s.origin_class, s.remembered_on, s.domain_tags,
               a.name AS agent_name, a.metadata, a.operator_id
        FROM sources s JOIN agents a ON a.id = s.agent_id
        WHERE s.store_id = $1
        """,
        store_id,
    )
    if row is None:
        return None
    return StoreSource(
        store_id=row["store_id"], source_id=row["source_id"], agent_id=row["agent_id"],
        operator_id=row["operator_id"], agent_name=row["agent_name"], text=row["text"],
        speaker=row["speaker"], addressee=row["addressee"], channel=row["channel"],
        origin_class=row["origin_class"], remembered_on=row["remembered_on"],
        domain_tags=list(row["domain_tags"] or []), user_name=_user_name(row["metadata"]),
    )


# ── Running a store ─────────────────────────────────────────────────────────

async def _mark_failed(pool: asyncpg.Pool, src: StoreSource, reason: str, trace: str) -> None:
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE store_jobs SET status = 'failed', error = $1, completed_at = now()
                    WHERE store_id = $2
                    """,
                    reason, src.store_id,
                )
                # The source row holds the text; store_failures keeps the error
                # only. One row per store (latest attempt). The app role has no
                # UPDATE on store_failures, so replace rather than upsert.
                await conn.execute("DELETE FROM store_failures WHERE id = $1", src.store_id)
                await conn.execute(
                    """
                    INSERT INTO store_failures (id, agent_id, original_text, error)
                    VALUES ($1, $2, NULL, $3)
                    """,
                    src.store_id, src.agent_id, trace,
                )
    except Exception:
        logger.error("failed to record store failure %s", src.store_id, exc_info=True)


async def _lifecycle(pool: asyncpg.Pool, agent_id: UUID, atom_ids: list[UUID]) -> None:
    from .lifecycle_service import detect_lifecycle_relationships
    for atom_id in atom_ids:
        try:
            await detect_lifecycle_relationships(pool, agent_id, atom_id)
        except Exception:
            logger.warning("detect_lifecycle_relationships failed for atom %s", atom_id, exc_info=True)


async def run_store(pool: asyncpg.Pool, src: StoreSource) -> dict:
    """Decompose and store. Never raises. Returns an outcome dict with
    `status` ("complete" | "failed") and `http_status` (201 | 502 | 500)."""
    base = {"store_id": src.store_id, "source_id": src.source_id}
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE store_jobs SET status = 'decomposing', error = NULL, completed_at = NULL "
            "WHERE store_id = $1",
            src.store_id,
        )

    # No connection is held from here until persist.
    try:
        prepared = await atom_service.prepare_atoms(src.request(), origin_class=src.origin_class)
    except DecomposerError as exc:
        logger.warning("store %s: decomposer failed: %s", src.store_id, exc.reason)
        await _mark_failed(pool, src, exc.reason, traceback.format_exc())
        return {**base, "status": "failed", "http_status": 502, "error": exc.reason}
    except Exception:
        logger.exception("store %s: preparation failed", src.store_id)
        await _mark_failed(pool, src, "storage error", traceback.format_exc())
        return {**base, "status": "failed", "http_status": 500, "error": "storage error"}

    # Relations (Phase 2): candidates on one short connection, model calls with
    # none held, under the store's budget. Fail open: any error → no plan, and
    # the store proceeds exactly as without relations.
    plan = None
    if settings.relation_mode != "off" and prepared.atoms:
        from . import relation_service
        try:
            plan = await relation_service.prepare_relations(
                pool, src.agent_id, src.source_id, src.request(), prepared.atoms, origin_class=src.origin_class)
        except Exception:
            logger.exception("store %s: relation step failed; storing without relations", src.store_id)
            plan = None

    t_persist = time.monotonic()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await atom_service.persist_atoms(
                    conn, src.agent_id, prepared, src.domain_tags,
                    source_id=src.source_id, store_id=src.store_id, operator_id=src.operator_id,
                    plan=plan, note_date=src.remembered_on,
                )
                await conn.execute(
                    """
                    UPDATE store_jobs
                    SET status = 'complete', atoms_created = $1, completed_at = now(), error = NULL
                    WHERE store_id = $2
                    """,
                    result["atoms_created"], src.store_id,
                )
    except Exception:
        logger.exception("store %s: persist failed", src.store_id)
        await _mark_failed(pool, src, "storage error", traceback.format_exc())
        return {**base, "status": "failed", "http_status": 500, "error": "storage error"}

    timings = dict(prepared.timings_ms or {})
    if plan is not None:
        timings.update({f"relations_{k}": v for k, v in plan.timings_ms.items()})
    timings["persist"] = round((time.monotonic() - t_persist) * 1000)
    # Ids, counts and timings only — no text (spec rule 3).
    logger.info(
        "store_complete",
        extra={"event": "store_complete", "store_id": str(src.store_id), "agent_id": str(src.agent_id),
               "atoms_created": result["atoms_created"], "duplicates_merged": result["duplicates_merged"],
               "parts": len(prepared.usage) or 1, "timings_ms": timings,
               "relation_mode": plan.mode if plan else "off",
               "relations": result.get("relation_counts") or {},
               "relations_pending": len(result.get("relations_pending") or []),
               "relation_budget_exhausted": bool(plan and plan.budget_exhausted)},
    )

    if settings.lifecycle_detection_enabled and result["new_atom_ids"]:
        coro = _lifecycle(pool, src.agent_id, list(result["new_atom_ids"]))
        if settings.sync_store_for_tests:
            await coro
        else:
            spawn(coro)

    return {
        **base,
        "status": "complete",
        "http_status": 201,
        "atoms_created": result["atoms_created"],
        "duplicates_merged": result["duplicates_merged"],
        "atoms": result["atoms"],
        "relations_pending": [str(x) for x in result.get("relations_pending") or []],
        "warnings": prepared.warnings,
        "timings_ms": timings,
    }


# ── Status, auth, retry, startup sweep ──────────────────────────────────────

async def job_visible_to(conn: asyncpg.Connection, store_id: UUID, auth) -> asyncpg.Record | None:
    """The job row if the caller may see it: the owning agent's key, the
    owning operator's key, or admin. Otherwise None (caller returns 404)."""
    row = await conn.fetchrow(
        """
        SELECT sj.store_id, sj.agent_id, sj.status, sj.atoms_created, sj.created_at,
               sj.completed_at, sj.error, a.operator_id AS agent_operator_id,
               s.id AS source_id
        FROM store_jobs sj
        JOIN agents a ON a.id = sj.agent_id
        LEFT JOIN sources s ON s.store_id = sj.store_id
        WHERE sj.store_id = $1
        """,
        store_id,
    )
    if row is None:
        return None
    if auth.role == "admin":
        return row
    if auth.role == "agent" and auth.agent_id == row["agent_id"]:
        return row
    if auth.role == "operator" and auth.operator_id == row["agent_operator_id"]:
        return row
    return None


async def store_atoms(conn: asyncpg.Connection, source_id: UUID | None) -> list[dict]:
    """New atoms (atoms.source_id) then merged-into atoms (source_merges)."""
    if source_id is None:
        return []
    rows = await conn.fetch(
        """
        SELECT a.id, a.atom_type, a.text_content, a.remembered_on, a.source_quote,
               false AS merged, a.verification, a.created_at AS ord
        FROM atoms a WHERE a.source_id = $1
        UNION ALL
        SELECT a.id, a.atom_type, a.text_content, a.remembered_on, m.source_quote,
               true AS merged, NULL::jsonb AS verification, m.created_at AS ord
        FROM source_merges m JOIN atoms a ON a.id = m.atom_id
        WHERE m.source_id = $1
        ORDER BY merged, ord
        """,
        source_id,
    )
    out = []
    for r in rows:
        v = r["verification"]
        if isinstance(v, str):
            v = json.loads(v)
        out.append({
            "id": r["id"], "atom_type": r["atom_type"], "text_content": r["text_content"],
            "remembered_on": r["remembered_on"], "source_quote": r["source_quote"],
            "merged_into_existing": r["merged"], "flags": list((v or {}).get("flags", [])),
        })
    return out


async def sweep_stuck_jobs(pool: asyncpg.Pool) -> int:
    """Startup: every job still pending/decomposing was interrupted by the
    restart (stores run in-process; nothing survives it), so all of them
    become failed and can be retried. Runs before the server takes requests."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE store_jobs
            SET status = 'failed', error = 'interrupted (server restart)', completed_at = now()
            WHERE status IN ('pending', 'decomposing')
            """
        )
    n = int(result.split()[-1])
    if n:
        logger.warning("marked %d stuck store job(s) as failed", n)
    return n
