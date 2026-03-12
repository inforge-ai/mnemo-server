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
import os
from uuid import UUID

import asyncpg
import numpy as np

from ..config import settings
from ..embeddings import encode
from ..decomposer import decompose as regex_decompose, infer_edges, DecomposedAtom

logger = logging.getLogger(__name__)

# Decay half-lives by atom type (days)
HALF_LIVES: dict[str, float] = {
    "episodic": settings.decay_episodic,
    "semantic": settings.decay_semantic,
    "procedural": settings.decay_procedural,
    "relational": settings.decay_relational,
}


async def _decompose(text: str, domain_tags: list[str] | None = None) -> list[DecomposedAtom]:
    """Use LLM decomposer if ANTHROPIC_API_KEY is set, else fall back to regex."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        from ..llm_decomposer import llm_decompose
        return await llm_decompose(text)
    return regex_decompose(text, domain_tags)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _atom_row_to_dict(row: asyncpg.Record) -> dict:
    return dict(row)


async def _check_duplicate(
    conn: asyncpg.Connection,
    agent_id: UUID,
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
          AND is_active = true
          AND 1 - (embedding <=> $1::vector) > $3
        ORDER BY similarity DESC
        LIMIT 1
        """,
        embedding,
        agent_id,
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
    decomposer_version: str = "regex_v1",
) -> asyncpg.Record:
    half_life = HALF_LIVES.get(atom.atom_type, 30.0)
    row = await conn.fetchrow(
        """
        INSERT INTO atoms (
            agent_id, atom_type, text_content, structured, embedding,
            confidence_alpha, confidence_beta,
            source_type, source_ref,
            domain_tags, decay_half_life_days, decay_type, decomposer_version
        ) VALUES ($1,$2,$3,$4,$5::vector,$6,$7,$8,$9,$10,$11,'exponential',$12)
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
        decomposer_version,
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


def _apply_gap_threshold(atoms: list[dict], threshold: float | None) -> list[dict]:
    """Stop returning results when score drops by more than `threshold` fraction."""
    if not threshold or len(atoms) <= 1:
        return atoms
    result = [atoms[0]]
    for i in range(1, len(atoms)):
        prev = atoms[i - 1]["relevance_score"] or 0.0
        curr = atoms[i]["relevance_score"] or 0.0
        if prev > 0 and (prev - curr) / prev > threshold:
            break
        result.append(atoms[i])
    return result


def _apply_token_budget(
    atoms: list[dict], max_tokens: int | None
) -> tuple[list[dict], int | None]:
    """Filter atoms to fit within a token budget (chars/4). Always returns at least 1.
    Returns (filtered_atoms, remaining_budget). remaining_budget is None if no budget."""
    if max_tokens is None:
        return atoms, None
    budget = float(max_tokens)
    result = []
    for atom in atoms:
        cost = len(atom["text_content"]) / 4
        if budget - cost < 0 and result:
            break
        budget -= cost
        result.append(atom)
    return result, max(0, int(budget))


def _dedup_results(rows: list, threshold: float = 0.95) -> list:
    """Collapse near-duplicate atoms (>threshold cosine similarity).
    Keeps the first occurrence (highest-ranked after sorting).
    Uses embeddings already fetched in the retrieval query."""
    if len(rows) <= 1:
        return rows

    kept = []
    dropped_ids: set = set()

    for i, row in enumerate(rows):
        if row["id"] in dropped_ids:
            continue
        kept.append(row)
        emb_i = row.get("embedding")
        if emb_i is None:
            continue
        for j in range(i + 1, len(rows)):
            if rows[j]["id"] in dropped_ids:
                continue
            emb_j = rows[j].get("embedding")
            if emb_j is None:
                continue
            sim = _cosine_sim(list(emb_i), list(emb_j))
            if sim > threshold:
                dropped_ids.add(rows[j]["id"])

    return kept


def _apply_verbosity(atoms: list[dict], verbosity: str, max_chars: int) -> list[dict]:
    """Compress text_content according to verbosity mode."""
    if verbosity == "full":
        return atoms
    for atom in atoms:
        text = atom["text_content"]
        if verbosity == "summary":
            end = text.find(". ")
            if end > 0:
                atom["text_content"] = text[: end + 1]
        elif verbosity == "truncated" and len(text) > max_chars:
            atom["text_content"] = text[:max_chars].rstrip() + "..."
    return atoms


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

async def _create_similarity_edges(
    conn: asyncpg.Connection,
    stored_ids: list[UUID],
    embeddings: list[list[float]],
    threshold: float = 0.7,
) -> int:
    """Create 'related' edges between atoms from the same /remember call
    that have cosine similarity above threshold. Preserves graph connectivity
    without depending on type classification."""
    edges_created = 0
    for i in range(len(stored_ids)):
        for j in range(i + 1, len(stored_ids)):
            sim = _cosine_sim(embeddings[i], embeddings[j])
            if sim > threshold:
                try:
                    await conn.execute(
                        """
                        INSERT INTO edges (source_id, target_id, edge_type, weight)
                        VALUES ($1, $2, 'related', $3)
                        ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
                        """,
                        stored_ids[i],
                        stored_ids[j],
                        round(sim, 3),
                    )
                    edges_created += 1
                except Exception:
                    pass
    return edges_created


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
    decomposed = await _decompose(text, domain_tags)
    if not decomposed:
        return {"atoms": [], "atoms_created": 0, "edges_created": 0, "duplicates_merged": 0}

    decomposer_version = "haiku_v1" if os.environ.get("ANTHROPIC_API_KEY") else "regex_v1"

    stored_ids: list[UUID] = []
    stored_rows: list[asyncpg.Record] = []
    stored_embeddings: list[list[float]] = []
    atoms_created = 0
    duplicates_merged = 0

    for atom in decomposed:
        embedding = await encode(atom.text)
        duplicate = await _check_duplicate(conn, agent_id, embedding)

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
            stored_embeddings.append(embedding)
            duplicates_merged += 1
        else:
            row = await _insert_atom(
                conn, agent_id, atom, embedding, domain_tags,
                atom.source_type, decomposer_version=decomposer_version,
            )
            stored_ids.append(row["id"])
            stored_rows.append(row)
            stored_embeddings.append(embedding)
            atoms_created += 1

    # Create similarity-based edges between atoms in this /remember call
    edges_created = await _create_similarity_edges(conn, stored_ids, stored_embeddings)

    # Also create arc→non-arc 'summarises' edges (arc atoms need structural links)
    arc_idxs = [i for i, a in enumerate(decomposed) if a.source_type == "arc"]
    non_arc_idxs = [i for i, a in enumerate(decomposed) if a.source_type != "arc"]
    for arc_idx in arc_idxs:
        for other_idx in non_arc_idxs:
            src_id = stored_ids[arc_idx]
            tgt_id = stored_ids[other_idx]
            if src_id == tgt_id:
                continue
            try:
                await conn.execute(
                    """
                    INSERT INTO edges (source_id, target_id, edge_type, weight)
                    VALUES ($1, $2, 'summarises', 1.0)
                    ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
                    """,
                    src_id,
                    tgt_id,
                )
                edges_created += 1
            except Exception:
                pass

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
    duplicate = await _check_duplicate(conn, agent_id, embedding)

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
    domain_tags: list[str] | None,
    min_confidence: float,
    min_similarity: float,
    max_results: int,
    expand_graph: bool,
    expansion_depth: int,
    include_superseded: bool,
    similarity_drop_threshold: float | None = 0.3,
    verbosity: str = "full",
    max_content_chars: int = 200,
    max_total_tokens: int | None = None,
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
            embedding,
            1 - (embedding <=> $1::vector) AS cosine_sim,
            effective_confidence(
                confidence_alpha, confidence_beta,
                decay_type, decay_half_life_days,
                created_at, last_accessed, access_count
            ) AS confidence_effective
        FROM atoms
        WHERE agent_id = $2
          AND is_active = true
          AND ($3::text[] IS NULL OR domain_tags && $3)
        ORDER BY cosine_sim DESC
        LIMIT $4
        """,
        embedding,
        agent_id,
        domain_tags,
        over_fetch,
    )

    # Apply similarity floor, confidence filter, compute composite score, sort.
    rows = [r for r in rows if r["cosine_sim"] >= min_similarity]
    rows = [r for r in rows if r["confidence_effective"] >= min_confidence]

    # Filter superseded atoms unless requested
    if not include_superseded:
        rows = await _filter_superseded(conn, rows)

    rows = _dedup_results(rows)

    rows.sort(
        key=lambda r: r["cosine_sim"] * (0.7 + 0.3 * r["confidence_effective"]),
        reverse=True,
    )

    primary = rows[:max_results]
    primary_ids = [r["id"] for r in primary]

    # Build primary responses and apply gap threshold before graph expansion.
    # Gap threshold is applied first so that atoms cut from primary are eligible
    # to surface in expanded_atoms (they must not be in exclude_ids).
    primary_responses = [
        _row_to_atom_response(r, r["cosine_sim"] * (0.7 + 0.3 * r["confidence_effective"]))
        for r in primary
    ]
    primary_responses = _apply_gap_threshold(primary_responses, similarity_drop_threshold)
    kept_ids = {a["id"] for a in primary_responses}

    # Update access timestamps for all pre-threshold primary atoms (not just survivors)
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
    # Use only post-threshold atoms as expansion seeds. Atoms cut by the gap threshold
    # are not seeds, so they can surface in expanded_atoms if connected to a surviving atom.
    kept_id_list = list(kept_ids) or primary_ids  # fall back to all if threshold cut everything
    if expand_graph and kept_id_list:
        from .graph_service import expand_graph as graph_expand
        expanded_rows = await graph_expand(
            conn=conn,
            agent_id=agent_id,
            seed_ids=kept_id_list,
            depth=expansion_depth,
            scope_filter=None,
            exclude_ids=kept_ids,  # only exclude surviving primary atoms from expanded results
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

    # Apply token budget (Change 3) — primary first, remainder to expanded
    primary_responses, remaining_budget = _apply_token_budget(primary_responses, max_total_tokens)
    expanded_responses, _ = _apply_token_budget(expanded_responses, remaining_budget)

    # Apply verbosity (Change 2) — compress per-atom text
    primary_responses = _apply_verbosity(primary_responses, verbosity, max_content_chars)
    expanded_responses = _apply_verbosity(expanded_responses, verbosity, max_content_chars)

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
            COUNT(*) FILTER (WHERE source_type='arc' AND is_active=true)      AS arc_atoms,
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
        "arc_atoms": row["arc_atoms"],
        "total_edges": edge_count,
        "avg_effective_confidence": float(row["avg_effective_confidence"]),
        "active_views": view_count,
        "granted_capabilities": granted_count,
        "received_capabilities": received_count,
    }
