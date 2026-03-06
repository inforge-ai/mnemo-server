"""
Operations logging — fire-and-forget per-call audit for:
  remember, recall, recall_shared, export_skill
"""

import json
import logging
from uuid import UUID

logger = logging.getLogger(__name__)


async def log_operation(
    conn,
    operation: str,
    agent_id,
    target_id=None,
    duration_ms: int | None = None,
    metadata: dict | None = None,
) -> None:
    """Insert one row into operations. Errors are swallowed — never block a real response."""
    try:
        await conn.execute(
            """
            INSERT INTO operations (agent_id, operation, target_id, duration_ms, metadata)
            VALUES ($1, $2, $3, $4, $5)
            """,
            UUID(str(agent_id)) if agent_id else None,
            operation,
            UUID(str(target_id)) if target_id else None,
            duration_ms,
            json.dumps(metadata or {}),
        )
    except Exception:
        logger.exception("ops_service.log_operation failed (ignored)")
