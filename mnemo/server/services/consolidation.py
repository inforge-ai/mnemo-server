import asyncio
import logging

import asyncpg

from ..config import settings

logger = logging.getLogger(__name__)


async def run_consolidation(pool: asyncpg.Pool) -> dict:
    """Placeholder — full implementation in Phase 7."""
    return {"decayed": 0, "clustered": 0, "merged": 0, "purged": 0}


async def consolidation_loop(pool: asyncpg.Pool) -> None:
    while True:
        await asyncio.sleep(settings.consolidation_interval_minutes * 60)
        try:
            result = await run_consolidation(pool)
            logger.info("Consolidation run: %s", result)
        except Exception:
            logger.exception("Consolidation run failed")
