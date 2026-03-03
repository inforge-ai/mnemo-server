"""
Graph expansion via recursive CTEs.

CRITICAL: When expanding within a shared view, expansion MUST be bounded
by the view's atom filter. No edge can pull in an atom outside the granted scope.

expand_graph(seed_ids, depth, scope_filter=None)

If scope_filter is None (agent's own retrieval): expand freely across all of
the agent's active atoms.

If scope_filter is provided (shared view retrieval): every expanded atom must
also match the view's filter. This prevents graph edges from leaking atoms
outside the view scope.
"""

import logging
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


async def expand_graph(
    conn: asyncpg.Connection,
    agent_id: UUID,
    seed_ids: list[UUID],
    depth: int,
    scope_filter: dict | None,
    exclude_ids: set[UUID] | None = None,
) -> list[asyncpg.Record]:
    """
    Follow edges up to `depth` hops from seed_ids and return expanded atoms.

    scope_filter: {"atom_types": [...], "domain_tags": [...]} or None
    exclude_ids: atoms already in the primary result set (not re-returned)
    """
    if not seed_ids or depth <= 0:
        return []

    exclude_ids = exclude_ids or set()

    scope_atom_types: list[str] | None = None
    scope_domain_tags: list[str] | None = None
    if scope_filter:
        scope_atom_types = scope_filter.get("atom_types") or None
        scope_domain_tags = scope_filter.get("domain_tags") or None

    rows = await conn.fetch(
        """
        WITH RECURSIVE expanded AS (
            -- Seed
            SELECT
                a.id,
                0 AS depth,
                1.0::float AS relevance
            FROM atoms a
            WHERE a.id = ANY($1)
              AND a.agent_id = $2
              AND a.is_active = true

            UNION

            -- Recursive step
            SELECT
                CASE
                    WHEN e.source_id = ex.id THEN e.target_id
                    ELSE e.source_id
                END AS id,
                ex.depth + 1 AS depth,
                ex.relevance * e.weight * 0.7 AS relevance
            FROM expanded ex
            JOIN edges e
              ON (e.source_id = ex.id OR e.target_id = ex.id)
            JOIN atoms a
              ON a.id = CASE
                            WHEN e.source_id = ex.id THEN e.target_id
                            ELSE e.source_id
                        END
            WHERE ex.depth < $3
              AND a.agent_id = $2
              AND a.is_active = true
              -- Scope boundary: restrict expansion to view filter if provided
              AND ($4::text[] IS NULL OR a.atom_type = ANY($4))
              AND ($5::text[] IS NULL OR a.domain_tags && $5)
        )
        SELECT DISTINCT ON (a.id)
            a.id, a.agent_id, a.atom_type, a.text_content, a.structured,
            a.confidence_alpha, a.confidence_beta,
            a.source_type, a.domain_tags, a.created_at,
            a.last_accessed, a.access_count, a.is_active,
            effective_confidence(
                a.confidence_alpha, a.confidence_beta,
                a.decay_type, a.decay_half_life_days,
                a.created_at, a.last_accessed, a.access_count
            ) AS confidence_effective,
            ex.depth,
            ex.relevance
        FROM expanded ex
        JOIN atoms a ON a.id = ex.id
        WHERE a.is_active = true
          AND a.id != ALL($1)   -- exclude seeds
          AND a.id != ALL($6)   -- exclude caller-specified IDs
        ORDER BY a.id, ex.relevance DESC
        """,
        seed_ids,
        agent_id,
        depth,
        scope_atom_types,
        scope_domain_tags,
        list(exclude_ids),
    )

    return list(rows)
