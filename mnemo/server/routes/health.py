"""
Health check endpoints.

GET /v1/health          — public, no auth required
GET /v1/health/detailed — admin-only, includes resource counts and config
"""

import os
import time

from fastapi import APIRouter, Depends, Request

from ..config import settings
from ..database import get_conn
from ..routes.admin import _require_admin

router = APIRouter(tags=["health"])


async def _basic_health(request: Request) -> dict:
    """Gather basic health info shared by both endpoints."""
    app = request.app
    version = app.version

    # Uptime
    start_time = getattr(app.state, "start_time", None)
    uptime_seconds = int(time.time() - start_time) if start_time else 0

    # Postgres connectivity + schema version
    pg_status = "ok"
    schema_version = "unknown"
    try:
        async with get_conn() as conn:
            await conn.fetchval("SELECT 1")
            try:
                schema_version = await conn.fetchval(
                    "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
                ) or "unknown"
            except Exception:
                schema_version = "unknown"
    except Exception:
        pg_status = "unreachable"

    status = "ok" if pg_status == "ok" else "degraded"

    return {
        "status": status,
        "version": version,
        "schema_version": schema_version,
        "uptime_seconds": uptime_seconds,
        "postgres": pg_status,
    }


@router.get("/health")
async def health(request: Request):
    return await _basic_health(request)


@router.get("/health/detailed", dependencies=[Depends(_require_admin)])
async def health_detailed(request: Request):
    info = await _basic_health(request)

    try:
        async with get_conn() as conn:
            # Sharing enabled
            sharing_row = await conn.fetchval(
                "SELECT value FROM platform_config WHERE key = 'sharing_enabled'"
            )
            sharing_enabled = sharing_row == "true" if sharing_row is not None else False

            # Counts
            operator_count = await conn.fetchval(
                "SELECT COUNT(*) FROM operators WHERE status = 'active'"
            )
            agent_count = await conn.fetchval(
                "SELECT COUNT(*) FROM agents WHERE status = 'active'"
            )
            atom_count = await conn.fetchval(
                "SELECT COUNT(*) FROM atoms WHERE is_active"
            )

            # Postgres version
            pg_version = await conn.fetchval("SHOW server_version")

            # pgvector version
            pgvector_version = await conn.fetchval(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ) or "unknown"

    except Exception:
        # If DB queries fail, return what we have with defaults
        sharing_enabled = False
        operator_count = 0
        agent_count = 0
        atom_count = 0
        pg_version = "unknown"
        pgvector_version = "unknown"

    # Decomposer type
    decomposer = "haiku" if os.environ.get("ANTHROPIC_API_KEY") else "regex"

    info.update({
        "sharing_enabled": sharing_enabled,
        "operator_count": operator_count,
        "agent_count": agent_count,
        "atom_count": atom_count,
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.embedding_dim,
        "postgres_version": pg_version,
        "pgvector_version": pgvector_version,
        "config": {
            "min_similarity": settings.duplicate_similarity_threshold,
            "decomposer": decomposer,
        },
    })

    return info
