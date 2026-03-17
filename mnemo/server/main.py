import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import create_pool, close_pool, set_pool

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .embeddings import warmup
    await asyncio.get_event_loop().run_in_executor(None, warmup)

    if os.environ.get("ANTHROPIC_API_KEY"):
        logger.info("LLM decomposer active (Haiku)")
    else:
        logger.warning("ANTHROPIC_API_KEY not set — falling back to regex decomposer")

    pool = await create_pool()
    set_pool(pool)

    from .services.consolidation import consolidation_loop
    task = asyncio.create_task(consolidation_loop(pool))

    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await close_pool()


app = FastAPI(title="Mnemo", version="0.2.0", lifespan=lifespan)


@app.get("/v1/health")
async def health():
    return {"status": "ok"}


def _register_routers():
    from .routes import admin, agents, atoms, auth, capabilities, memory, views
    app.include_router(auth.router, prefix="/v1")
    app.include_router(agents.router, prefix="/v1")
    app.include_router(memory.router, prefix="/v1")
    app.include_router(atoms.router, prefix="/v1")
    app.include_router(views.router, prefix="/v1")
    app.include_router(capabilities.router, prefix="/v1")
    app.include_router(admin.router, prefix="/v1")


_register_routers()
