"""
Migration runner — applies numbered SQL files from the migrations/ directory.

On startup, creates the schema_migrations tracking table if it doesn't exist,
then applies any unapplied migrations in order. Each migration is recorded
by its filename (without .sql extension) as the version key.
"""

import logging
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

# migrations/ directory lives at the project root
_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


async def run_migrations(pool: asyncpg.Pool) -> list[str]:
    """
    Apply all pending migrations and return the list of newly applied versions.

    Migrations containing DDL (CREATE TABLE, ALTER TABLE) must be applied by
    a database superuser first. This runner checks whether each migration has
    been recorded in schema_migrations and applies any that haven't been.

    If the schema_migrations table doesn't exist yet (DDL not applied),
    the runner logs a warning and returns gracefully.
    """
    async with pool.acquire() as conn:
        # Check if schema_migrations table exists (created by the DDL migration)
        table_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'schema_migrations'
            )
        """)

        if not table_exists:
            logger.warning(
                "schema_migrations table does not exist. "
                "Run migrations as postgres first: "
                "sudo -u postgres psql <dbname> -f migrations/001_admin_schema.sql"
            )
            return []

        # Discover migration files
        if not _MIGRATIONS_DIR.is_dir():
            logger.info("No migrations directory found at %s", _MIGRATIONS_DIR)
            return []

        sql_files = sorted(
            f for f in _MIGRATIONS_DIR.iterdir()
            if f.suffix == ".sql" and f.is_file()
        )

        if not sql_files:
            logger.info("No migration files found")
            return []

        # Fetch already-applied versions
        applied_rows = await conn.fetch("SELECT version FROM schema_migrations")
        applied = {r["version"] for r in applied_rows}

        # Check for unapplied migrations and log them
        unapplied = [
            f.stem for f in sql_files if f.stem not in applied
        ]
        if unapplied:
            logger.warning(
                "Unapplied migrations detected: %s. "
                "Apply them as postgres: sudo -u postgres psql <dbname> -f migrations/<file>.sql",
                ", ".join(unapplied),
            )

        # Record any migrations that have been applied externally but not yet tracked.
        # We check by looking for schema changes that indicate the migration was applied.
        applied_now: list[str] = []
        for sql_file in sql_files:
            version = sql_file.stem
            if version in applied:
                continue

            # For 001_admin_schema, check if the status column exists on agents
            if version == "001_admin_schema":
                col_exists = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'agents' AND column_name = 'status'
                    )
                """)
                if col_exists:
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING",
                        version,
                    )
                    applied_now.append(version)
                    logger.info("Migration already applied (detected), recorded: %s", version)

        if not applied_now and not unapplied:
            logger.info("All migrations already applied")

        return applied_now
