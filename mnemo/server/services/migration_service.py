"""
Migration tracking — numbered SQL files in the migrations/ directory.

A migration's version is its filename without `.sql` (e.g.
`007_deactivation_tracking`). Applied versions are recorded in
`schema_migrations`. Rows there that match no file (legacy short names such as
`002`) are ignored.

- `mnemo migrate` applies pending files via `apply_pending`, each in its own
  transaction, using a role allowed to run DDL.
- Server startup calls `run_migrations`, which only *checks*: pending
  migrations are logged as an error and, when `settings.require_migrations` is
  true, startup fails. The server never runs DDL itself.
"""

import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

# migrations/ directory lives at the project root
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
_MIGRATIONS_DIR = MIGRATIONS_DIR  # backward-compatible alias


class MigrationsDirectoryMissingError(RuntimeError):
    def __init__(self, directory: Path):
        self.directory = directory
        super().__init__(f"Migrations directory not found: {directory}")


class PendingMigrationsError(RuntimeError):
    def __init__(self, pending: list[str]):
        self.pending = pending
        super().__init__(
            f"Unapplied migrations: {', '.join(pending)}. Run `mnemo migrate`."
        )


def migration_files(directory: Path | None = None) -> list[Path]:
    """All migrations/*.sql files in filename order."""
    d = directory or MIGRATIONS_DIR
    if not d.is_dir():
        return []
    return sorted(f for f in d.iterdir() if f.suffix == ".sql" and f.is_file())


async def applied_versions(conn: asyncpg.Connection) -> set[str] | None:
    """Versions recorded in schema_migrations, or None if the table is missing."""
    exists = await conn.fetchval("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
    if not exists:
        return None
    rows = await conn.fetch("SELECT version FROM schema_migrations")
    return {r["version"] for r in rows}


async def list_pending(conn: asyncpg.Connection, directory: Path | None = None) -> list[str]:
    """Versions of migration files not recorded in schema_migrations, in order.

    If schema_migrations does not exist, every file is pending.
    """
    applied = await applied_versions(conn) or set()
    return [f.stem for f in migration_files(directory) if f.stem not in applied]


async def apply_pending(
    conn: asyncpg.Connection,
    directory: Path | None = None,
    dry_run: bool = False,
    to: str | None = None,
) -> list[str]:
    """Apply each pending migration in its own transaction and record it.

    `to` stops after the named version (inclusive); it must be a known file.

    Stops at the first failure (that migration's transaction is rolled back,
    earlier ones stay committed) and re-raises.
    """
    if await applied_versions(conn) is None:
        if dry_run:
            return [f.stem for f in migration_files(directory)]
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(128) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

    files = {f.stem: f for f in migration_files(directory)}
    pending = await list_pending(conn, directory)
    if to is not None:
        if to not in files:
            raise ValueError(f"Unknown migration version: {to}")
        pending = [v for v in pending if v <= to]
    if dry_run:
        return pending

    applied_now: list[str] = []
    for version in pending:
        sql = files[version].read_text()
        logger.info("Applying migration %s", version)
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING",
                version,
            )
        applied_now.append(version)
    return applied_now


async def run_migrations(pool: asyncpg.Pool) -> list[str]:
    """Startup check: return pending migrations; fail if required.

    Does not apply anything.
    """
    from ..config import settings

    # A missing directory means the check cannot be done at all (e.g. the
    # image was built without migrations/). Never report that as "all applied".
    if not MIGRATIONS_DIR.is_dir():
        logger.error("Migrations directory not found: %s", MIGRATIONS_DIR)
        if settings.require_migrations:
            raise MigrationsDirectoryMissingError(MIGRATIONS_DIR)
        return []

    async with pool.acquire() as conn:
        pending = await list_pending(conn)

    if not pending:
        logger.info("All migrations applied")
        return []

    logger.error("Unapplied migrations: %s. Run `mnemo migrate`.", ", ".join(pending))
    if settings.require_migrations:
        raise PendingMigrationsError(pending)
    return pending
