import asyncpg
from pgvector.asyncpg import register_vector
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from .config import settings

_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=10,
        command_timeout=30,
        init=_init_connection,
    )
    return pool


async def _init_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        raise RuntimeError("Database pool not initialised")
    return _pool


def set_pool(pool: asyncpg.Pool) -> None:
    global _pool
    _pool = pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def get_conn() -> AsyncGenerator[asyncpg.Connection, None]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
