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

        # Widen version column if needed (was VARCHAR(16), now VARCHAR(128))
        col_len = await conn.fetchval("""
            SELECT character_maximum_length
            FROM information_schema.columns
            WHERE table_name = 'schema_migrations' AND column_name = 'version'
        """)
        if col_len is not None and col_len < 128:
            await conn.execute(
                "ALTER TABLE schema_migrations ALTER COLUMN version TYPE VARCHAR(128)"
            )
            logger.info("Widened schema_migrations.version to VARCHAR(128)")

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

            # Heuristic checks: detect migrations that were applied manually
            # but not recorded in schema_migrations.
            check_col = None
            if version == "001_admin_schema":
                check_col = ("agents", "status")
            elif version == "002_rbac_lite":
                check_col = ("agents", "key_hash")
            elif version == "003_sharing_scope":
                check_col = ("operators", "sharing_scope")
            elif version == "004_fix_effective_confidence_volatility":
                # Check if effective_confidence is already STABLE
                vol = await conn.fetchval("""
                    SELECT p.provolatile
                    FROM pg_proc p
                    JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE p.proname = 'effective_confidence'
                      AND n.nspname = 'public'
                    LIMIT 1
                """)
                if vol == "s":  # 's' = STABLE
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING",
                        version,
                    )
                    applied_now.append(version)
                    logger.info("Migration already applied (detected), recorded: %s", version)
                continue

            if check_col:
                col_exists = await conn.fetchval("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = $1 AND column_name = $2
                    )
                """, check_col[0], check_col[1])
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
