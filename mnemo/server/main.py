import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .database import create_pool, close_pool, set_pool
from .logging_config import configure_logging

logger = logging.getLogger(__name__)


def check_decomposer_config() -> None:
    """Refuse to start with a decomposer that cannot work. There is no
    silent regex fallback in v2: regex runs only when chosen explicitly."""
    provider = settings.decomposer_provider
    if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "MNEMO_DECOMPOSER_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set. "
            "Set the key, or set MNEMO_DECOMPOSER_PROVIDER=regex explicitly."
        )
    if provider == "openrouter" and not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("MNEMO_DECOMPOSER_PROVIDER=openrouter but OPENROUTER_API_KEY is not set.")
    logger.info("Decomposer: %s %s", provider, settings.decomposer_model if provider in ("anthropic", "openrouter") else "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(level=settings.log_level)

    check_decomposer_config()

    from .embeddings import warmup
    await asyncio.get_event_loop().run_in_executor(None, warmup)

    if not settings.admin_key:
        logger.warning("MNEMO_ADMIN_KEY not set — admin endpoints will be inaccessible")

    pool = await create_pool()
    set_pool(pool)

    from .services.migration_service import run_migrations
    await run_migrations(pool)

    # Before serving requests: any unfinished store was interrupted by the restart.
    from .services.store_service import drain_background, sweep_stuck_jobs
    await sweep_stuck_jobs(pool)

    from .services.consolidation import consolidation_loop
    task = asyncio.create_task(consolidation_loop(pool))
    from .services.relation_service import pending_loop
    relation_task = asyncio.create_task(pending_loop(pool))

    app.state.start_time = time.time()

    yield

    for t in (task, relation_task):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    await drain_background(timeout=10.0)
    await close_pool()


from .version import get_version

app = FastAPI(title="Mnemo", version=get_version(), lifespan=lifespan)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://admin.mnemo-ai.com"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-Admin-Key", "X-Admin-Token", "X-Agent-Key", "X-Operator-Key", "Content-Type"],
    max_age=86400,
)


def _register_routers():
    from .routes import admin, admin_agents, admin_operators, admin_trust, agents, atoms, auth, capabilities, health, memory, shares, views
    app.include_router(health.router, prefix="/v1")
    app.include_router(auth.router, prefix="/v1")
    app.include_router(agents.router, prefix="/v1")
    app.include_router(memory.router, prefix="/v1")
    app.include_router(atoms.router, prefix="/v1")
    app.include_router(views.router, prefix="/v1")
    app.include_router(capabilities.router, prefix="/v1")
    app.include_router(shares.router, prefix="/v1")
    app.include_router(admin.router, prefix="/v1")
    app.include_router(admin_operators.router, prefix="/v1")
    app.include_router(admin_agents.router, prefix="/v1")
    app.include_router(admin_trust.router, prefix="/v1")


_register_routers()
