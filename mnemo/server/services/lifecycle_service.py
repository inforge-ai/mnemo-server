"""Lifecycle relationship detection (docs/episodic_suppression-tension.md).

Four-way classifier: supersedes / tension_with / narrows / independent.
Three are edge-creating; "independent" is a no-op. Runs from
atom_service.store_background after the store transaction commits, gated
by settings.lifecycle_detection_enabled. Failure mode: log and skip;
permanent failures land in lifecycle_dlq.
"""

import logging
from uuid import UUID

import asyncpg

from ..config import settings

logger = logging.getLogger(__name__)


async def _get_candidates(
    conn: asyncpg.Connection,
    agent_id: UUID,
    new_atom_id: UUID,
    embedding: list[float],
) -> list[dict]:
    """ANN-query active same-agent atoms, filter to the lifecycle cosine band."""
    over_fetch = max(settings.lifecycle_candidate_limit * 4, 20)
    rows = await conn.fetch(
        """
        SELECT id, text_content, atom_type, remembered_on, created_at,
               1 - (embedding <=> $1::vector) AS similarity
        FROM atoms
        WHERE agent_id = $2
          AND is_active = true
          AND id != $3
        ORDER BY embedding <=> $1::vector
        LIMIT $4
        """,
        embedding,
        agent_id,
        new_atom_id,
        over_fetch,
    )
    candidates = []
    for r in rows:
        sim = float(r["similarity"])
        if settings.lifecycle_band_low <= sim < settings.lifecycle_band_high:
            candidates.append({
                "id": r["id"],
                "text_content": r["text_content"],
                "atom_type": r["atom_type"],
                "remembered_on": r["remembered_on"],
                "created_at": r["created_at"],
                "similarity": sim,
            })
        if len(candidates) >= settings.lifecycle_candidate_limit:
            break
    return candidates
