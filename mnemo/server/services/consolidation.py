"""
Background consolidation job. Runs on a schedule (default: every 60 min).

CONSOLIDATION STEPS:
1. DECAY        — deactivate atoms with effective_confidence < 0.05
2. CLUSTER      — find 3+ episodic atoms with similarity > 0.85 (same agent, domain overlap)
3. GENERALISE   — create semantic atom from each cluster; 'generalises' edges to members
4. MERGE DUPES  — find pairs with similarity > 0.90, same agent/type; keep older, Bayesian merge
4b. PRUNE EDGES — delete edges where either endpoint is now inactive
5. PURGE        — delete atoms/views for departed agents past data_expires_at
6. LOG          — record run metadata in access_log
"""

import asyncio
import json
import logging
from uuid import UUID

import asyncpg
import numpy as np

from ..config import settings
from .atom_service import bayesian_merge_damped

logger = logging.getLogger(__name__)

# Synthetic system agent UUID for consolidation audit log entries.
# access_log.agent_id has no FK constraint, so any UUID is valid.
_SYSTEM_AGENT_ID = UUID("00000000-0000-0000-0000-000000000001")

# PostgreSQL advisory lock key — ensures only one consolidation process runs at a time.
_CONSOLIDATION_LOCK_KEY = 42


# ── Public entry points ────────────────────────────────────────────────────────

async def run_consolidation(pool: asyncpg.Pool) -> dict:
    """
    Run all consolidation steps and return counts.

    Uses a PostgreSQL advisory lock to ensure only one consolidation process
    runs at a time. If the lock is already held, returns immediately with
    skipped=True. Each step runs in its own explicit transaction so that a
    failure in one step does not roll back the work done by earlier steps.
    """
    async with pool.acquire() as conn:
        locked = await conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", _CONSOLIDATION_LOCK_KEY
        )
        if not locked:
            logger.info("Consolidation already running, skipping this run")
            return {"skipped": True, "decayed": 0, "clustered": 0, "merged": 0, "pruned": 0, "purged": 0}

        try:
            async with conn.transaction():
                decayed = await _deactivate_faded_atoms(conn)

            async with conn.transaction():
                clustered = await _cluster_and_generalise(conn)

            async with conn.transaction():
                merged = await _merge_duplicates(conn)

            async with conn.transaction():
                pruned = await _prune_dead_edges(conn)

            async with conn.transaction():
                purged = await _purge_departed_agents(conn)

            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO access_log (agent_id, action, metadata)
                    VALUES ($1, 'consolidation', $2)
                    """,
                    _SYSTEM_AGENT_ID,
                    json.dumps({
                        "decayed": decayed,
                        "clustered": clustered,
                        "merged": merged,
                        "pruned": pruned,
                        "purged": purged,
                    }),
                )
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock($1)", _CONSOLIDATION_LOCK_KEY
            )

    logger.info(
        "Consolidation: decayed=%d, clustered=%d, merged=%d, pruned=%d, purged=%d",
        decayed, clustered, merged, pruned, purged,
    )
    return {
        "decayed": decayed,
        "clustered": clustered,
        "merged": merged,
        "pruned": pruned,
        "purged": purged,
    }


async def consolidation_loop(pool: asyncpg.Pool) -> None:
    """Infinite loop: sleep → run → repeat."""
    while True:
        await asyncio.sleep(settings.consolidation_interval_minutes * 60)
        try:
            result = await run_consolidation(pool)
            logger.info("Consolidation run: %s", result)
        except Exception:
            logger.exception("Consolidation run failed")


# ── Step 1: Decay ─────────────────────────────────────────────────────────────

async def _deactivate_faded_atoms(conn: asyncpg.Connection) -> int:
    result = await conn.execute(
        """
        UPDATE atoms SET is_active = false
        WHERE is_active = true
          AND effective_confidence(
              confidence_alpha, confidence_beta,
              decay_type, decay_half_life_days,
              created_at, last_accessed, access_count
          ) < $1
        """,
        settings.min_effective_confidence,
    )
    count = int(result.split()[-1])
    if count:
        logger.info("Decay: deactivated %d atoms", count)
    return count


# ── Step 2 & 3: Cluster episodic atoms → generalise ──────────────────────────

async def _cluster_and_generalise(conn: asyncpg.Connection) -> int:
    """
    Find groups of 3+ similar episodic atoms (same agent, domain overlap,
    cosine > 0.85). For each group, create a generalised semantic atom with
    edges pointing back to the cluster members.
    Atoms already covered by a prior generalisation edge are skipped.
    """
    pairs = await conn.fetch(
        """
        SELECT a1.id AS id1, a2.id AS id2, a1.agent_id
        FROM atoms a1
        JOIN atoms a2
          ON a1.agent_id = a2.agent_id
         AND a1.id < a2.id
         AND a2.atom_type = 'episodic'
         AND a2.is_active = true
         AND a1.domain_tags && a2.domain_tags
         AND 1 - (a1.embedding <=> a2.embedding) > 0.85
        WHERE a1.atom_type = 'episodic'
          AND a1.is_active = true
          -- Only process atoms not recently consolidated (optimisation for large sets)
          AND (a1.last_consolidated_at IS NULL
               OR a1.last_consolidated_at < now() - interval '1 hour')
          -- Skip atoms that were already generalised by a prior consolidation run
          AND NOT EXISTS (
              SELECT 1 FROM edges e
              JOIN atoms src ON src.id = e.source_id
              WHERE e.target_id = a1.id
                AND e.edge_type = 'generalises'
                AND src.source_type = 'consolidation'
          )
          AND NOT EXISTS (
              SELECT 1 FROM edges e
              JOIN atoms src ON src.id = e.source_id
              WHERE e.target_id = a2.id
                AND e.edge_type = 'generalises'
                AND src.source_type = 'consolidation'
          )
        """,
    )

    # Stamp processed atoms regardless of whether clusters were found
    await conn.execute(
        """
        UPDATE atoms SET last_consolidated_at = now()
        WHERE atom_type = 'episodic'
          AND is_active = true
          AND (last_consolidated_at IS NULL
               OR last_consolidated_at < now() - interval '1 hour')
        """,
    )

    if not pairs:
        return 0

    # Union-find to identify connected components
    components = _union_find(
        [(str(p["id1"]), str(p["id2"])) for p in pairs]
    )

    # Track which agent owns each node
    node_agents: dict[str, UUID] = {}
    for p in pairs:
        node_agents[str(p["id1"])] = p["agent_id"]
        node_agents[str(p["id2"])] = p["agent_id"]

    generalised_count = 0
    for root, member_ids in components.items():
        if len(member_ids) < 3:
            continue

        agent_id = node_agents[root]
        atom_rows = await conn.fetch(
            """
            SELECT id, text_content, confidence_alpha, confidence_beta,
                   domain_tags, embedding
            FROM atoms
            WHERE id = ANY($1)
            """,
            [UUID(m) for m in member_ids],
        )

        # Centroid embedding (normalised mean)
        embeddings = np.array([np.array(r["embedding"]) for r in atom_rows])
        centroid = embeddings.mean(axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm

        # Most confident source atom provides the anchor text
        most_conf = max(
            atom_rows,
            key=lambda r: r["confidence_alpha"] / (r["confidence_alpha"] + r["confidence_beta"]),
        )

        # Union of domain tags
        all_tags: set[str] = set()
        for r in atom_rows:
            all_tags.update(r["domain_tags"])

        # Confidence: sum alphas (evidence accumulation), max beta (uncertainty floor)
        total_alpha = sum(r["confidence_alpha"] for r in atom_rows)
        max_beta = max(r["confidence_beta"] for r in atom_rows)

        text = (
            f"Generalised from {len(member_ids)} observations: "
            f"{most_conf['text_content']}"
        )

        new_atom = await conn.fetchrow(
            """
            INSERT INTO atoms (
                agent_id, atom_type, text_content, embedding,
                confidence_alpha, confidence_beta, source_type,
                domain_tags, decay_type, decay_half_life_days
            ) VALUES ($1, 'semantic', $2, $3::vector, $4, $5, 'consolidation',
                      $6, 'none', 90.0)
            RETURNING id
            """,
            agent_id,
            text,
            centroid.tolist(),
            total_alpha,
            max_beta,
            list(all_tags),
        )
        new_id = new_atom["id"]

        for member_id in member_ids:
            await conn.execute(
                """
                INSERT INTO edges (source_id, target_id, edge_type, weight)
                VALUES ($1, $2, 'generalises', 1.0)
                ON CONFLICT DO NOTHING
                """,
                new_id,
                UUID(member_id),
            )

        generalised_count += 1
        logger.debug(
            "Cluster generalised: %d atoms → %s (agent %s)",
            len(member_ids), new_id, agent_id,
        )

    return generalised_count


# ── Step 4: Merge duplicate atoms ─────────────────────────────────────────────

async def _merge_duplicates(conn: asyncpg.Connection) -> int:
    """
    Find active atom pairs (same agent, same type, cosine > 0.90).
    Keep the older atom, Bayesian-merge confidence, reassign edges, deactivate newer.
    The merge event is recorded in access_log rather than as a graph edge.
    """
    pairs = await conn.fetch(
        """
        SELECT
            a1.id AS id1, a1.created_at AS created1,
            a1.confidence_alpha AS alpha1, a1.confidence_beta AS beta1,
            a1.agent_id AS agent_id,
            a2.id AS id2, a2.created_at AS created2,
            a2.confidence_alpha AS alpha2, a2.confidence_beta AS beta2
        FROM atoms a1
        JOIN atoms a2
          ON a1.agent_id = a2.agent_id
         AND a1.atom_type = a2.atom_type
         AND a1.id < a2.id
         AND a2.is_active = true
         AND 1 - (a1.embedding <=> a2.embedding) > 0.90
        WHERE a1.is_active = true
        ORDER BY a1.created_at ASC
        """,
    )

    if not pairs:
        return 0

    deactivated: set[UUID] = set()
    merged_count = 0

    for pair in pairs:
        id1, id2 = pair["id1"], pair["id2"]
        if id1 in deactivated or id2 in deactivated:
            continue

        agent_id = pair["agent_id"]

        # Older atom is the keeper
        if pair["created1"] <= pair["created2"]:
            older_id, newer_id = id1, id2
            older_alpha = pair["alpha1"]
            new_alpha, new_beta = bayesian_merge_damped(
                pair["alpha1"], pair["beta1"], pair["alpha2"], pair["beta2"],
            )
        else:
            older_id, newer_id = id2, id1
            older_alpha = pair["alpha2"]
            new_alpha, new_beta = bayesian_merge_damped(
                pair["alpha2"], pair["beta2"], pair["alpha1"], pair["beta1"],
            )

        # Update older atom's confidence
        await conn.execute(
            """
            UPDATE atoms
            SET confidence_alpha = $1, confidence_beta = $2, last_accessed = now()
            WHERE id = $3
            """,
            new_alpha, new_beta, older_id,
        )

        # Reassign source edges: delete conflicting ones, then remap the rest
        await conn.execute(
            """
            DELETE FROM edges
            WHERE source_id = $1
              AND (target_id, edge_type) IN (
                  SELECT target_id, edge_type FROM edges WHERE source_id = $2
              )
            """,
            newer_id, older_id,
        )
        await conn.execute(
            "UPDATE edges SET source_id = $1 WHERE source_id = $2",
            older_id, newer_id,
        )

        # Reassign target edges: delete conflicting, remap the rest
        await conn.execute(
            """
            DELETE FROM edges
            WHERE target_id = $1
              AND (source_id, edge_type) IN (
                  SELECT source_id, edge_type FROM edges WHERE target_id = $2
              )
            """,
            newer_id, older_id,
        )
        await conn.execute(
            "UPDATE edges SET target_id = $1 WHERE target_id = $2",
            older_id, newer_id,
        )

        # Record merge in access_log (no graph edge to deactivated atom)
        await conn.execute(
            """
            INSERT INTO access_log (agent_id, action, target_id, metadata)
            VALUES ($1, 'merge', $2, $3)
            """,
            agent_id,
            older_id,
            json.dumps({
                "absorbed_atom_id": str(newer_id),
                "alpha_before": older_alpha,
                "alpha_after": new_alpha,
            }),
        )

        # Deactivate the newer (merged) atom
        await conn.execute(
            "UPDATE atoms SET is_active = false WHERE id = $1",
            newer_id,
        )

        deactivated.add(newer_id)
        merged_count += 1
        logger.debug("Merged duplicate: %s absorbed into %s", newer_id, older_id)

    return merged_count


# ── Step 4b: Prune dead edges ─────────────────────────────────────────────────

async def _prune_dead_edges(conn: asyncpg.Connection) -> int:
    """
    Remove edges where either endpoint (source or target) is inactive.
    These accumulate as atoms are deactivated by decay and merges.
    """
    result = await conn.execute(
        """
        DELETE FROM edges
        WHERE source_id IN (SELECT id FROM atoms WHERE is_active = false)
           OR target_id IN (SELECT id FROM atoms WHERE is_active = false)
        """,
    )
    count = int(result.split()[-1])
    if count:
        logger.info("Pruned %d dead edges", count)
    return count


# ── Step 5: Purge departed agents ─────────────────────────────────────────────

async def _purge_departed_agents(conn: asyncpg.Connection) -> int:
    """
    Delete agents whose data_expires_at has passed.
    Capabilities (no ON DELETE CASCADE) are cleaned up first to avoid FK violations.
    Atoms, views, edges, snapshot_atoms all cascade from the agent delete.
    """
    expired = await conn.fetch(
        """
        SELECT id FROM agents
        WHERE data_expires_at IS NOT NULL
          AND data_expires_at < now()
        """,
    )
    if not expired:
        return 0

    expired_ids = [r["id"] for r in expired]

    # Remove capability references (no cascade on grantor_id / grantee_id)
    await conn.execute(
        """
        DELETE FROM capabilities
        WHERE grantor_id = ANY($1) OR grantee_id = ANY($1)
        """,
        expired_ids,
    )

    # Delete agents — cascades to atoms, views, edges, snapshot_atoms
    result = await conn.execute(
        "DELETE FROM agents WHERE id = ANY($1)",
        expired_ids,
    )
    count = int(result.split()[-1])
    if count:
        logger.info("Purge: deleted %d departed agents", count)
    return count


# ── Union-find helper ──────────────────────────────────────────────────────────

def _union_find(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    """
    Given a list of (id1, id2) pairs, return connected components as
    {representative_id: [member_ids]}.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        if x not in parent:
            parent[x] = x
        root = x
        while parent[root] != root:
            root = parent[root]
        # Path compression
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(x: str, y: str) -> None:
        parent[find(x)] = find(y)

    for x, y in pairs:
        union(x, y)

    groups: dict[str, list[str]] = {}
    all_nodes = set(node for pair in pairs for node in pair)
    for node in all_nodes:
        root = find(node)
        groups.setdefault(root, []).append(node)

    return groups
