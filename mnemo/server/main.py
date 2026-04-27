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


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(level=settings.log_level)

    from .embeddings import warmup
    await asyncio.get_event_loop().run_in_executor(None, warmup)

    if os.environ.get("ANTHROPIC_API_KEY"):
        logger.info("LLM decomposer active (Haiku)")
    else:
        logger.warning("ANTHROPIC_API_KEY not set — falling back to regex decomposer")

    if not settings.admin_key:
        logger.warning("MNEMO_ADMIN_KEY not set — admin endpoints will be inaccessible")

    pool = await create_pool()
    set_pool(pool)

    from .services.migration_service import run_migrations
    await run_migrations(pool)

    from .services.consolidation import consolidation_loop
    task = asyncio.create_task(consolidation_loop(pool))

    app.state.start_time = time.time()

    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
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
