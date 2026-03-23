"""Platform-level configuration checks."""


async def is_sharing_enabled(conn) -> bool:
    """Check if global sharing is enabled via platform_config.

    Defaults to True when no explicit config exists (sharing enabled by default).
    Only returns False when explicitly disabled via admin trust/disable.
    """
    row = await conn.fetchrow(
        "SELECT value FROM platform_config WHERE key = 'sharing_enabled'"
    )
    if row is None:
        return True  # sharing enabled by default
    val = row["value"]
    # asyncpg returns JSONB booleans as Python strings (e.g. "true"/"false")
    return val is True or val == "true"
