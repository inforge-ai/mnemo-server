"""
Core business logic for storing and retrieving atoms.

STORE FLOW (via /remember):
1. Decomposer breaks text into typed atoms
2. For each atom:
   a. Generate embedding
   b. Check for duplicates (cosine similarity > 0.90, same agent, same type)
   c. If duplicate: Bayesian update α_new = α_old + α_incoming - 1, update last_accessed
   d. If no duplicate: insert new atom with server-assigned decay half-life
3. Create edges between atoms from the same /remember call
4. Log the access

RETRIEVE FLOW (via /recall):
1. Generate embedding from query
2. Vector similarity search with effective_confidence applied at query time
3. Filter superseded atoms (unless include_superseded=True)
4. Optionally expand via graph edges (scope-bounded)
5. Update last_accessed and access_count on returned atoms
6. Return primary + expanded results
"""

import json
import logging
from uuid import UUID

import asyncpg
import numpy as np

from ..config import settings
from ..embeddings import encode
from ..decomposer import decompose, infer_edges, DecomposedAtom

logger = logging.getLogger(__name__)

# Decay half-lives by atom type (days)
HALF_LIVES: dict[str, float] = {
    "episodic": settings.decay_episodic,
    "semantic": settings.decay_semantic,
    "procedural": settings.decay_procedural,
    "relational": settings.decay_relational,
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _atom_row_to_dict(row: asyncpg.Record) -> dict:
    return dict(row)


async def _check_duplicate(
    conn: asyncpg.Connection,
    agent_id: UUID,
    atom_type: str,
    embedding: list[float],
) -> asyncpg.Record | None:
    """Return the most similar existing active atom if similarity > threshold."""
    threshold = settings.duplicate_similarity_threshold
    row = await conn.fetchrow(
        """
        SELECT id, confidence_alpha, confidence_beta,
               1 - (embedding <=> $1::vector) AS similarity
        FROM atoms
        WHERE agent_id = $2
          AND atom_type = $3
          AND is_active = true
          AND 1 - (embedding <=> $1::vector) > $4
        ORDER BY similarity DESC
        LIMIT 1
        """,
        embedding,
        agent_id,
        atom_type,
        threshold,
    )
    return row


async def _merge_duplicate(
    conn: asyncpg.Connection,
    existing_id: UUID,
    existing_alpha: float,
    existing_beta: float,
    incoming_alpha: float,
    incoming_beta: float,
) -> None:
    """Bayesian update: add evidence from the incoming atom into the existing one."""
    new_alpha = existing_alpha + incoming_alpha - 1.0
    new_beta = existing_beta + incoming_beta - 1.0
    # Clamp to sane minimums
    new_alpha = max(new_alpha, 1.0)
    new_beta = max(new_beta, 1.0)
    await conn.execute(
        """
        UPDATE atoms
        SET confidence_alpha = $1,
            confidence_beta  = $2,
            last_accessed    = now(),
            access_count     = access_count + 1
        WHERE id = $3
        """,
        new_alpha,
        new_beta,
        existing_id,
    )


async def _insert_atom(
    conn: asyncpg.Connection,
    agent_id: UUID,
    atom: DecomposedAtom,
    embedding: list[float],
    domain_tags: list[str],
    source_type: str = "direct_experience",
    source_ref: UUID | None = None,
) -> asyncpg.Record:
    half_life = HALF_LIVES.get(atom.atom_type, 30.0)
    row = await conn.fetchrow(
        """
        INSERT INTO atoms (
            agent_id, atom_type, text_content, structured, embedding,
            confidence_alpha, confidence_beta,
            source_type, source_ref,
            domain_tags, decay_half_life_days, decay_type
        ) VALUES ($1,$2,$3,$4,$5::vector,$6,$7,$8,$9,$10,$11,'exponential')
        RETURNING
            id, agent_id, atom_type, text_content, structured,
            confidence_alpha, confidence_beta,
            source_type, domain_tags, created_at, last_accessed, access_count, is_active,
            effective_confidence(
                confidence_alpha, confidence_beta,
                decay_type, decay_half_life_days,
                created_at, last_accessed, access_count
            ) AS confidence_effective
        """,
        agent_id,
        atom.atom_type,
        atom.text,
        json.dumps(atom.structured),
        embedding,
        atom.confidence_alpha,
        atom.confidence_beta,
        source_type,
        source_ref,
        domain_tags,
        half_life,
    )
    return row


async def _get_atom_row(
    conn: asyncpg.Connection,
    atom_id: UUID,
    agent_id: UUID,
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT
            id, agent_id, atom_type, text_content, structured,
            confidence_alpha, confidence_beta,
            source_type, domain_tags, created_at, last_accessed, access_count, is_active,
            effective_confidence(
                confidence_alpha, confidence_beta,
                decay_type, decay_half_life_days,
                created_at, last_accessed, access_count
            ) AS confidence_effective
        FROM atoms
        WHERE id = $1 AND agent_id = $2 AND is_active = true
        """,
        atom_id,
        agent_id,
    )


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two embedding vectors."""
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    norm = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(np.dot(va, vb)) / norm if norm > 0 else 0.0


def _row_to_atom_response(row: asyncpg.Record, relevance_score: float | None = None) -> dict:
    alpha = row["confidence_alpha"]
    beta = row["confidence_beta"]
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "atom_type": row["atom_type"],
        "text_content": row["text_content"],
        "structured": json.loads(row["structured"]) if isinstance(row["structured"], str) else (row["structured"] or {}),
        "confidence_expected": alpha / (alpha + beta),
        "confidence_effective": row["confidence_effective"],
        "relevance_score": relevance_score,
        "source_type": row["source_type"],
        "domain_tags": list(row["domain_tags"]) if row["domain_tags"] else [],
        "created_at": row["created_at"],
        "last_accessed": row["last_accessed"],
        "access_count": row["access_count"],
        "is_active": row["is_active"],
    }


# ── Public API ────────────────────────────────────────────────────────────────

async def store_from_text(
    conn: asyncpg.Connection,
    agent_id: UUID,
    text: str,
    domain_tags: list[str],
) -> dict:
    """
    Decompose free text into atoms, deduplicate, store, and link.
    Returns {"atoms": [...], "atoms_created": N, "edges_created": N, "duplicates_merged": N}
    """
    decomposed = decompose(text, domain_tags)
    if not decomposed:
        return {"atoms": [], "atoms_created": 0, "edges_created": 0, "duplicates_merged": 0}

    stored_ids: list[UUID] = []
    stored_rows: list[asyncpg.Record] = []
    atoms_created = 0
    duplicates_merged = 0

    for atom in decomposed:
        embedding = await encode(atom.text)
        duplicate = await _check_duplicate(conn, agent_id, atom.atom_type, embedding)

        if duplicate:
            await _merge_duplicate(
                conn,
                duplicate["id"],
                duplicate["confidence_alpha"],
                duplicate["confidence_beta"],
                atom.confidence_alpha,
                atom.confidence_beta,
            )
            stored_ids.append(duplicate["id"])
            row = await _get_atom_row(conn, duplicate["id"], agent_id)
            stored_rows.append(row)
            duplicates_merged += 1
        else:
            row = await _insert_atom(conn, agent_id, atom, embedding, domain_tags)
            stored_ids.append(row["id"])
            stored_rows.append(row)
            atoms_created += 1

    # Create edges between atoms in this /remember call
    edges_created = 0
    edge_pairs = infer_edges(decomposed)
    for src_idx, tgt_idx, edge_type in edge_pairs:
        src_id = stored_ids[src_idx]
        tgt_id = stored_ids[tgt_idx]
        if src_id == tgt_id:
            continue
        try:
            await conn.execute(
                """
                INSERT INTO edges (source_id, target_id, edge_type, weight)
                VALUES ($1, $2, $3, 1.0)
                ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
                """,
                src_id,
                tgt_id,
                edge_type,
            )
            edges_created += 1
        except Exception:
            logger.exception("Failed to create edge %s -> %s (%s)", src_id, tgt_id, edge_type)

    return {
        "atoms": [_row_to_atom_response(r) for r in stored_rows],
        "atoms_created": atoms_created,
        "edges_created": edges_created,
        "duplicates_merged": duplicates_merged,
    }


async def store_explicit(
    conn: asyncpg.Connection,
    agent_id: UUID,
    atom_type: str,
    text_content: str,
    structured: dict,
    confidence_label: str | None,
    source_type: str,
    source_ref: UUID | None,
    domain_tags: list[str],
) -> dict:
    """Create an explicit typed atom (power-user interface)."""
    # Map confidence label to Beta params
    label_map = {
        "high": (8.0, 1.0),
        "medium": (4.0, 2.0),
        "low": (2.0, 3.0),
        "uncertain": (2.0, 4.0),
    }
    if confidence_label and confidence_label in label_map:
        alpha, beta = label_map[confidence_label]
    else:
        alpha, beta = (4.0, 2.0)

    embedding = await encode(text_content)
    duplicate = await _check_duplicate(conn, agent_id, atom_type, embedding)

    if duplicate:
        await _merge_duplicate(
            conn,
            duplicate["id"],
            duplicate["confidence_alpha"],
            duplicate["confidence_beta"],
            alpha,
            beta,
        )
        row = await _get_atom_row(conn, duplicate["id"], agent_id)
    else:
        from ..decomposer import DecomposedAtom
        atom = DecomposedAtom(
            text=text_content,
            atom_type=atom_type,
            confidence_alpha=alpha,
            confidence_beta=beta,
            structured=structured,
        )
        row = await _insert_atom(conn, agent_id, atom, embedding, domain_tags, source_type, source_ref)

    return _row_to_atom_response(row)


async def retrieve(
    conn: asyncpg.Connection,
    agent_id: UUID,
    query: str,
    atom_types: list[str] | None,
    domain_tags: list[str] | None,
    min_confidence: float,
    min_similarity: float,
    max_results: int,
    expand_graph: bool,
    expansion_depth: int,
    include_superseded: bool,
) -> dict:
    """
    Semantic retrieval with decay filtering, access updates, optional graph expansion.

    Ranking: composite score = similarity * (0.7 + 0.3 * effective_confidence).
    Similarity floor: atoms below min_similarity are excluded from primary results.
    Expansion floor: expanded atoms below min_similarity * 0.6 are excluded.
    """
    embedding = await encode(query)
    over_fetch = max_results * 2

    # Fetch top candidates using the ivfflat index (ORDER BY distance ASC).
    # Similarity floor, confidence filter, and composite ranking are applied
    # in Python to avoid naming conflicts with pg_trgm's similarity() function.
    rows = await conn.fetch(
        """
        SELECT
            id, agent_id, atom_type, text_content, structured,
            confidence_alpha, confidence_beta,
            source_type, domain_tags, created_at, last_accessed, access_count, is_active,
            1 - (embedding <=> $1::vector) AS cosine_sim,
            effective_confidence(
                confidence_alpha, confidence_beta,
                decay_type, decay_half_life_days,
                created_at, last_accessed, access_count
            ) AS confidence_effective
        FROM atoms
        WHERE agent_id = $2
          AND is_active = true
          AND ($3::text[] IS NULL OR atom_type = ANY($3))
          AND ($4::text[] IS NULL OR domain_tags && $4)
        ORDER BY cosine_sim DESC
        LIMIT $5
        """,
        embedding,
        agent_id,
        atom_types,
        domain_tags,
        over_fetch,
    )

    # Apply similarity floor, confidence filter, compute composite score, sort.
    rows = [r for r in rows if r["cosine_sim"] >= min_similarity]
    rows = [r for r in rows if r["confidence_effective"] >= min_confidence]

    # Filter superseded atoms unless requested
    if not include_superseded:
        rows = await _filter_superseded(conn, rows)

    rows.sort(
        key=lambda r: r["cosine_sim"] * (0.7 + 0.3 * r["confidence_effective"]),
        reverse=True,
    )

    primary = rows[:max_results]
    primary_ids = [r["id"] for r in primary]

    # Update access timestamps
    if primary_ids:
        await conn.execute(
            """
            UPDATE atoms
            SET last_accessed = now(), access_count = access_count + 1
            WHERE id = ANY($1)
            """,
            primary_ids,
        )

    expanded_responses: list[dict] = []
    if expand_graph and primary_ids:
        from .graph_service import expand_graph as graph_expand
        expanded_rows = await graph_expand(
            conn=conn,
            agent_id=agent_id,
            seed_ids=primary_ids,
            depth=expansion_depth,
            scope_filter=None,
            exclude_ids=set(primary_ids),
        )

        # Filter expanded atoms by query relevance (permissive floor = 60% of primary floor)
        # and sort by the same composite score.
        exp_floor = min_similarity * 0.6
        scored: list[tuple[asyncpg.Record, float]] = []
        for r in expanded_rows:
            sim = _cosine_sim(r["embedding"], embedding)
            if sim >= exp_floor:
                score = sim * (0.7 + 0.3 * r["confidence_effective"])
                scored.append((r, score))
        scored.sort(key=lambda x: x[1], reverse=True)

        expanded_ids = [r["id"] for r, _ in scored]
        if expanded_ids:
            await conn.execute(
                """
                UPDATE atoms
                SET last_accessed = now(), access_count = access_count + 1
                WHERE id = ANY($1)
                """,
                expanded_ids,
            )

        expanded_responses = [_row_to_atom_response(r, score) for r, score in scored]

    primary_responses = [
        _row_to_atom_response(r, r["cosine_sim"] * (0.7 + 0.3 * r["confidence_effective"]))
        for r in primary
    ]

    return {
        "atoms": primary_responses,
        "expanded_atoms": expanded_responses,
        "total_retrieved": len(primary_responses) + len(expanded_responses),
    }


async def _filter_superseded(
    conn: asyncpg.Connection,
    rows: list[asyncpg.Record],
) -> list[asyncpg.Record]:
    if not rows:
        return rows
    atom_ids = [r["id"] for r in rows]
    superseded = await conn.fetch(
        """
        SELECT DISTINCT e.target_id
        FROM edges e
        JOIN atoms a2 ON a2.id = e.source_id
        WHERE e.target_id = ANY($1)
          AND e.edge_type = 'supersedes'
          AND a2.is_active = true
        """,
        atom_ids,
    )
    superseded_ids = {r["target_id"] for r in superseded}
    return [r for r in rows if r["id"] not in superseded_ids]


async def get_atom(
    conn: asyncpg.Connection,
    agent_id: UUID,
    atom_id: UUID,
) -> dict | None:
    row = await _get_atom_row(conn, atom_id, agent_id)
    if row is None:
        return None
    return _row_to_atom_response(row)


async def soft_delete_atom(
    conn: asyncpg.Connection,
    agent_id: UUID,
    atom_id: UUID,
) -> bool:
    result = await conn.execute(
        """
        UPDATE atoms SET is_active = false
        WHERE id = $1 AND agent_id = $2 AND is_active = true
        """,
        atom_id,
        agent_id,
    )
    return result != "UPDATE 0"


async def create_edge(
    conn: asyncpg.Connection,
    source_id: UUID,
    target_id: UUID,
    edge_type: str,
    weight: float,
) -> dict | None:
    row = await conn.fetchrow(
        """
        INSERT INTO edges (source_id, target_id, edge_type, weight)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
        RETURNING id, source_id, target_id, edge_type, weight
        """,
        source_id,
        target_id,
        edge_type,
        weight,
    )
    return dict(row) if row else None


async def get_agent_stats(
    conn: asyncpg.Connection,
    agent_id: UUID,
) -> dict:
    row = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE true)                        AS total_atoms,
            COUNT(*) FILTER (WHERE is_active = true)            AS active_atoms,
            COUNT(*) FILTER (WHERE atom_type='episodic'  AND is_active=true) AS episodic,
            COUNT(*) FILTER (WHERE atom_type='semantic'  AND is_active=true) AS semantic,
            COUNT(*) FILTER (WHERE atom_type='procedural' AND is_active=true) AS procedural,
            COUNT(*) FILTER (WHERE atom_type='relational' AND is_active=true) AS relational,
            COALESCE(AVG(
                CASE WHEN is_active THEN
                    effective_confidence(
                        confidence_alpha, confidence_beta,
                        decay_type, decay_half_life_days,
                        created_at, last_accessed, access_count
                    )
                END
            ), 0.0) AS avg_effective_confidence
        FROM atoms
        WHERE agent_id = $1
        """,
        agent_id,
    )

    edge_count = await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM edges e
        JOIN atoms a ON a.id = e.source_id
        WHERE a.agent_id = $1
        """,
        agent_id,
    )

    view_count = await conn.fetchval(
        "SELECT COUNT(*) FROM views WHERE owner_agent_id = $1",
        agent_id,
    )

    granted_count = await conn.fetchval(
        "SELECT COUNT(*) FROM capabilities WHERE grantor_id = $1 AND revoked = false",
        agent_id,
    )

    received_count = await conn.fetchval(
        "SELECT COUNT(*) FROM capabilities WHERE grantee_id = $1 AND revoked = false",
        agent_id,
    )

    return {
        "agent_id": agent_id,
        "total_atoms": row["total_atoms"],
        "active_atoms": row["active_atoms"],
        "atoms_by_type": {
            "episodic": row["episodic"],
            "semantic": row["semantic"],
            "procedural": row["procedural"],
            "relational": row["relational"],
        },
        "total_edges": edge_count,
        "avg_effective_confidence": float(row["avg_effective_confidence"]),
        "active_views": view_count,
        "granted_capabilities": granted_count,
        "received_capabilities": received_count,
    }
