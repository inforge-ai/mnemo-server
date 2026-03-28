"""Platform-level configuration and sharing scope checks."""

from enum import Enum
from uuid import UUID

from fastapi import HTTPException


class SharingScope(str, Enum):
    NONE = "none"
    INTRA = "intra"
    FULL = "full"


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


async def check_sharing_allowed(
    conn,
    operator_id: UUID,
    target_agent_id: UUID | None = None,
) -> None:
    """
    Enforce per-operator sharing scope. Call before any sharing operation.

    - scope='none' → 403 on all sharing operations
    - scope='intra' → 403 if target_agent_id belongs to a different operator
    - scope='full' → no additional checks

    Raises HTTPException(403) if the operation is not allowed.
    """
    row = await conn.fetchrow(
        "SELECT sharing_scope FROM operators WHERE id = $1",
        operator_id,
    )
    if row is None:
        raise HTTPException(status_code=403, detail="Operator not found")

    scope = row["sharing_scope"]

    if scope == SharingScope.NONE:
        raise HTTPException(
            status_code=403,
            detail="Sharing is not enabled for this account. "
                   "Contact your administrator to enable sharing.",
        )

    if scope == SharingScope.INTRA and target_agent_id is not None:
        target_row = await conn.fetchrow(
            "SELECT operator_id FROM agents WHERE id = $1",
            target_agent_id,
        )
        if target_row is None:
            raise HTTPException(status_code=404, detail="Target agent not found")
        if target_row["operator_id"] != operator_id:
            raise HTTPException(
                status_code=403,
                detail="Cross-operator sharing is not available on this plan. "
                       "Your agents can share with each other within your account.",
            )

    # scope == 'full' or intra with same operator → allowed
