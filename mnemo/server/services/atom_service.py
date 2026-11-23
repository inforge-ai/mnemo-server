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
import math
import time
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import asyncpg
import numpy as np

from ..config import settings
from ..embeddings import encode, encode_batch
from ..decomposer import DecomposedAtom
from ..decomposers import CandidateAtom, DecomposeRequest, beta_for, decompose_note
from ..decomposers.base import lower_one_band
from . import grounding

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
    embedding: list[float],
    atom_type: str = "semantic",
) -> asyncpg.Record | None:
    """Return the most similar existing active atom of the same type if similarity > threshold."""
    threshold = settings.duplicate_similarity_threshold
    row = await conn.fetchrow(
        """
        SELECT id, confidence_alpha, confidence_beta,
               1 - (embedding <=> $1::vector) AS similarity
        FROM atoms
        WHERE agent_id = $2
          AND is_active = true
          AND atom_type = $4
          AND 1 - (embedding <=> $1::vector) > $3
        ORDER BY embedding <=> $1::vector ASC
        LIMIT 1
        """,
        embedding,
        agent_id,
        threshold,
        atom_type,
    )
    return row


MERGE_CEILING = 1000.0


def bayesian_merge_damped(
    existing_alpha: float,
    existing_beta: float,
    incoming_alpha: float,
    incoming_beta: float,
    ceiling: float = MERGE_CEILING,
) -> tuple[float, float]:
    """Combine two Beta posteriors with diminishing returns on accumulated evidence.

    The classical conjugate update is α_new = α_old + α_inc − 1 (symmetric for β).
    In production that compounded without bound on dedup magnets — α reached
    198,429 on a single atom — so each update is damped by 1/(1+ln(α+β)) of the
    existing total. Later merges therefore contribute progressively less than
    earlier ones, preserving "seen a few times" vs "seen many times" while
    bounding the head of the distribution. A hard ceiling is a backstop against
    edge cases; it should almost never bind under the damped law.
    """
    total = existing_alpha + existing_beta
    damping = 1.0 / (1.0 + math.log(total)) if total > 1.0 else 1.0
    new_alpha = existing_alpha + (incoming_alpha - 1.0) * damping
    new_beta = existing_beta + (incoming_beta - 1.0) * damping
    new_alpha = min(ceiling, max(1.0, new_alpha))
    new_beta = min(ceiling, max(1.0, new_beta))
    return new_alpha, new_beta


async def _merge_duplicate(
    conn: asyncpg.Connection,
    existing_id: UUID,
    existing_alpha: float,
    existing_beta: float,
    incoming_alpha: float,
    incoming_beta: float,
) -> None:
    """Damped Bayesian update: add evidence from the incoming atom into the existing one.

    See bayesian_merge_damped for the math. The update is persisted immediately
    and reflected in the next recall via effective_confidence().
    """
    new_alpha, new_beta = bayesian_merge_damped(
        existing_alpha, existing_beta, incoming_alpha, incoming_beta,
    )
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
    source_id: UUID | None = None,
    source_quote: str | None = None,
    verification: dict | None = None,
) -> asyncpg.Record:
    half_life = HALF_LIVES.get(atom.atom_type, 30.0)
    row = await conn.fetchrow(
        """
        INSERT INTO atoms (
            agent_id, atom_type, text_content, structured, embedding,
            confidence_alpha, confidence_beta,
            source_type, source_ref,
            domain_tags, decay_half_life_days, decay_type, decomposer_version,
            remembered_on, source_id, source_quote, verification
        ) VALUES ($1,$2,$3,$4,$5::vector,$6,$7,$8,$9,$10,$11,'exponential',$12,$13,$14,$15,$16)
        RETURNING
            id, agent_id, atom_type, text_content, structured,
            confidence_alpha, confidence_beta,
            source_type, domain_tags, created_at, last_accessed, access_count, is_active,
            remembered_on, source_id, source_quote,
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
        atom.remembered_on,
        source_id,
        source_quote,
        json.dumps(verification) if verification is not None else None,
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
            a.id, a.agent_id, a.atom_type, a.text_content, a.structured,
            a.confidence_alpha, a.confidence_beta,
            a.source_type, a.domain_tags, a.created_at, a.last_accessed, a.access_count, a.is_active,
            a.remembered_on, a.source_id, s.origin_class,
            effective_confidence(
                a.confidence_alpha, a.confidence_beta,
                a.decay_type, a.decay_half_life_days,
                a.created_at, a.last_accessed, a.access_count
            ) AS confidence_effective
        FROM atoms a
        LEFT JOIN sources s ON s.id = a.source_id
        WHERE a.id = $1 AND a.agent_id = $2 AND a.is_active = true
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
        atom.pop("confidence_alpha", None)
        atom.pop("confidence_beta", None)
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


def composite_score(similarity: float, confidence_effective: float, source_type: str) -> float:
    """Composite ranking score with specificity penalty for consolidated atoms."""
    base = similarity * (0.7 + 0.3 * confidence_effective)
    if source_type == "consolidation":
        base *= 0.85  # 15% penalty — broad embeddings over-match
    return base


def apply_episodic_recency_demotion(
    rows: list[asyncpg.Record],
    scores: dict[UUID, float],
) -> dict[UUID, float]:
    """Demote older episodic atoms when a newer near-duplicate is present.

    Ticket 4b. For each pair of episodic atoms A and B in the candidate set,
    if cosine similarity between their embeddings is above the threshold AND
    B has a newer event-date than A, multiply A's composite score by the
    demotion factor. Non-episodic atoms are untouched. NULL remembered_on
    falls back to created_at (per the leave-NULL migration choice).

    Returns a new id→score dict so the caller can re-sort.
    """
    threshold = settings.episodic_recency_similarity_threshold
    factor = settings.episodic_recency_demotion_factor
    episodic = [r for r in rows if r["atom_type"] == "episodic"]
    if len(episodic) < 2:
        return scores

    def recency_key(r: asyncpg.Record):
        return _safe_get(r, "remembered_on") or r["created_at"]

    out = dict(scores)
    for a in episodic:
        a_time = recency_key(a)
        a_emb = a["embedding"]
        for b in episodic:
            if b["id"] == a["id"]:
                continue
            b_time = recency_key(b)
            if b_time <= a_time:
                continue
            sim = _cosine_sim(list(a_emb), list(b["embedding"]))
            if sim >= threshold:
                out[a["id"]] = out.get(a["id"], 0.0) * factor
                break  # one demotion is enough; don't compound
    return out


def _row_to_atom_response(
    row: asyncpg.Record,
    relevance_score: float | None = None,
    match_type: str | None = None,
    via: UUID | None = None,
) -> dict:
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
        "confidence_alpha": alpha,
        "confidence_beta": beta,
        "match_type": match_type,
        "via": via,
        # remembered_on is present on rows from atom inserts/gets/retrieves
        # and graph expansions; other SELECT paths may omit it — fall back to None.
        "remembered_on": _safe_get(row, "remembered_on"),
        # v2 Phase 1 provenance (NULL for atoms stored before Phase 1).
        "source_id": _safe_get(row, "source_id"),
        "origin_class": _safe_get(row, "origin_class"),
    }


def _safe_get(row: asyncpg.Record, key: str):
    try:
        return row[key]
    except (KeyError, ValueError):
        return None


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
            # Two decomposed atoms can dedup into the same existing atom;
            # never link an atom to itself.
            if stored_ids[i] == stored_ids[j]:
                continue
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
                    pass  # best-effort: skip duplicate/constraint violations
    return edges_created


async def _create_cross_call_edges(
    conn: asyncpg.Connection,
    agent_id: UUID,
    new_atom_id: UUID,
    embedding: list[float],
    exclude_ids: set[UUID],
) -> int:
    """Query existing atoms via ANN and create 'related' edges for any above threshold.

    This connects atoms across /remember calls. Only creates edges — never merges
    or updates existing atoms. See spec §1: "Edges Only, Not Merging".

    Uses ORDER BY distance ASC (not computed similarity DESC) so pgvector can
    use the ivfflat index. Threshold filtering is applied in Python.
    """
    threshold = settings.cross_call_edge_threshold
    # Use the ivfflat-friendly ORDER BY pattern; over-fetch then filter by threshold
    rows = await conn.fetch(
        """
        SELECT id, 1 - (embedding <=> $1::vector) AS similarity
        FROM atoms
        WHERE agent_id = $2
          AND is_active = true
          AND id != ALL($3::uuid[])
        ORDER BY embedding <=> $1::vector
        LIMIT 10
        """,
        embedding,
        agent_id,
        list(exclude_ids),
    )

    edges_created = 0
    for row in rows:
        if row["similarity"] < threshold:
            continue
        try:
            await conn.execute(
                """
                INSERT INTO edges (source_id, target_id, edge_type, weight)
                VALUES ($1, $2, 'related', $3)
                ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
                """,
                new_atom_id,
                row["id"],
                round(row["similarity"], 3),
            )
            edges_created += 1
        except Exception:
            logger.debug("Failed to create cross-call edge %s -> %s", new_atom_id, row["id"], exc_info=True)
    return edges_created


@dataclass
class PreparedAtom:
    candidate: CandidateAtom
    embedding: list[float]
    flags: list[str]
    alpha: float
    beta: float
    band_downgraded: bool


@dataclass
class PreparedStore:
    atoms: list[PreparedAtom]
    usage: list[dict]
    warnings: list[str]
    version: str
    timings_ms: dict | None = None   # {"decompose": ms, "embed": ms}


async def prepare_atoms(req: DecomposeRequest) -> PreparedStore:
    """Decompose, check and embed a note. Holds NO database connection: the
    model call and the embedding run before any connection is acquired.

    Raises DecomposerError (plain reason) if decomposition fails."""
    t0 = time.monotonic()
    result = await decompose_note(req)
    t1 = time.monotonic()
    candidates = result.atoms
    embeddings = await encode_batch([c.text for c in candidates])
    t2 = time.monotonic()
    prepared: list[PreparedAtom] = []
    for cand, emb in zip(candidates, embeddings):
        known = tuple(n for n in (req.speaker, req.addressee, req.agent_name, req.user_name) if n)
        flags = grounding.run_checks(cand.text, cand.source_quote, req.text, known)
        alpha, beta = beta_for(cand)
        downgraded = False
        if grounding.lowers_band(flags):
            lowered = lower_one_band(alpha, beta)
            downgraded = lowered != (alpha, beta)
            alpha, beta = lowered
        prepared.append(PreparedAtom(cand, emb, flags, alpha, beta, downgraded))
    return PreparedStore(
        prepared, result.usage, result.warnings, result.version,
        timings_ms={"decompose": round((t1 - t0) * 1000), "embed": round((t2 - t1) * 1000)},
    )


async def persist_atoms(
    conn: asyncpg.Connection,
    agent_id: UUID,
    prepared: PreparedStore,
    domain_tags: list[str],
    source_id: UUID | None = None,
    store_id: UUID | None = None,
    operator_id: UUID | None = None,
) -> dict:
    """Write a prepared store. The caller wraps this in ONE transaction:
    dedup checks, inserts, source_merges, edges and usage rows commit or
    roll back together.

    Returns {"atoms": [store item], "atoms_created", "duplicates_merged",
    "edges_created", "new_atom_ids"}."""
    if store_id and operator_id:
        for usage in prepared.usage:
            await conn.execute(
                """
                INSERT INTO decomposer_usage (
                    store_id, operator_id, agent_id, model,
                    input_tokens, cache_creation_input_tokens,
                    cache_read_input_tokens, output_tokens
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                store_id, operator_id, agent_id, usage["model"],
                usage["input_tokens"], usage.get("cache_creation_input_tokens"),
                usage.get("cache_read_input_tokens"), usage["output_tokens"],
            )

    stored_ids: list[UUID] = []
    stored_embeddings: list[list[float]] = []
    items: list[dict] = []
    new_atom_indices: list[int] = []
    atoms_created = 0
    duplicates_merged = 0

    for p in prepared.atoms:
        cand = p.candidate
        # Arc atoms are synthetic summaries — never deduplicate them against
        # their component sentences (their embeddings overlap by design).
        duplicate = None if cand.source_type == "arc" else await _check_duplicate(
            conn, agent_id, p.embedding, cand.atom_type,
        )
        if duplicate:
            await _merge_duplicate(
                conn, duplicate["id"], duplicate["confidence_alpha"], duplicate["confidence_beta"],
                p.alpha, p.beta,
            )
            row = await _get_atom_row(conn, duplicate["id"], agent_id)
            if source_id is not None:
                await conn.execute(
                    """
                    INSERT INTO source_merges (source_id, atom_id, source_quote)
                    VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
                    """,
                    source_id, duplicate["id"], cand.source_quote,
                )
            stored_ids.append(duplicate["id"])
            stored_embeddings.append(p.embedding)
            items.append(_store_item(row, cand.source_quote, merged=True, flags=p.flags))
            duplicates_merged += 1
        else:
            structured = {"subject": cand.subject} if cand.subject else {}
            atom = DecomposedAtom(
                text=cand.text, atom_type=cand.atom_type,
                confidence_alpha=p.alpha, confidence_beta=p.beta,
                structured=structured, source_type=cand.source_type,
                remembered_on=cand.remembered_on,
            )
            row = await _insert_atom(
                conn, agent_id, atom, p.embedding, domain_tags, cand.source_type,
                decomposer_version=prepared.version or "unknown",
                source_id=source_id, source_quote=cand.source_quote,
                verification=grounding.verification_record(p.flags, p.band_downgraded),
            )
            stored_ids.append(row["id"])
            stored_embeddings.append(p.embedding)
            items.append(_store_item(row, cand.source_quote, merged=False, flags=p.flags))
            new_atom_indices.append(len(stored_ids) - 1)
            atoms_created += 1

    edges_created = await _create_similarity_edges(conn, stored_ids, stored_embeddings)

    # Arc → non-arc 'summarises' edges (arc atoms come from the regex provider).
    arc_idxs = [i for i, p in enumerate(prepared.atoms) if p.candidate.source_type == "arc"]
    non_arc_idxs = [i for i, p in enumerate(prepared.atoms) if p.candidate.source_type != "arc"]
    for arc_idx in arc_idxs:
        for other_idx in non_arc_idxs:
            src_id, tgt_id = stored_ids[arc_idx], stored_ids[other_idx]
            if src_id == tgt_id:
                continue
            await conn.execute(
                """
                INSERT INTO edges (source_id, target_id, edge_type, weight)
                VALUES ($1, $2, 'summarises', 1.0)
                ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
                """,
                src_id, tgt_id,
            )
            edges_created += 1

    # Cross-call edges: link NEW atoms to similar atoms from earlier stores.
    current_call_ids = set(stored_ids)
    for i in new_atom_indices:
        edges_created += await _create_cross_call_edges(
            conn, agent_id, stored_ids[i], stored_embeddings[i], current_call_ids,
        )

    return {
        "atoms": items,
        "atoms_created": atoms_created,
        "duplicates_merged": duplicates_merged,
        "edges_created": edges_created,
        "new_atom_ids": [stored_ids[i] for i in new_atom_indices],
    }


def _store_item(row: asyncpg.Record, source_quote: str | None, merged: bool, flags: list[str]) -> dict:
    """One entry of the remember / store-status `atoms` list."""
    return {
        "id": row["id"],
        "atom_type": row["atom_type"],
        "text_content": row["text_content"],
        "remembered_on": row["remembered_on"],
        "source_quote": source_quote,
        "merged_into_existing": merged,
        "flags": list(flags),
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
    remembered_on: datetime | None = None,
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
    duplicate = await _check_duplicate(conn, agent_id, embedding, atom_type)

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
            remembered_on=remembered_on,
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
    similarity_drop_threshold: float | None = 0.15,
    verbosity: str = "full",
    max_content_chars: int = 200,
    max_total_tokens: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    atom_types: list[str] | None = None,
) -> dict:
    """
    Semantic retrieval with decay filtering, access updates, optional graph expansion.

    since/until (on COALESCE(remembered_on, created_at); since inclusive,
    until exclusive) and atom_types are SQL WHERE filters applied before
    LIMIT, for both the vector candidates and graph-expanded atoms.

    Ranking: composite score = similarity * (0.7 + 0.3 * effective_confidence),
    with a 15% specificity penalty for consolidated atoms.
    Similarity floor: atoms below min_similarity are excluded from primary results.
    Expansion floor: expanded atoms below min_similarity * 0.6 are excluded.
    """
    embedding = await encode(query, prompt_name="query")
    over_fetch = max_results * 2

    # Fetch top candidates by exact cosine distance over the agent's atoms.
    # Similarity floor, confidence filter, and composite ranking are applied
    # in Python to avoid naming conflicts with pg_trgm's similarity() function.
    rows = await conn.fetch(
        """
        SELECT
            a.id, a.agent_id, a.atom_type, a.text_content, a.structured,
            a.confidence_alpha, a.confidence_beta,
            a.source_type, a.domain_tags, a.created_at, a.last_accessed, a.access_count, a.is_active,
            a.remembered_on, a.source_id, s.origin_class,
            a.embedding,
            1 - (a.embedding <=> $1::vector) AS cosine_sim,
            effective_confidence(
                a.confidence_alpha, a.confidence_beta,
                a.decay_type, a.decay_half_life_days,
                a.created_at, a.last_accessed, a.access_count
            ) AS confidence_effective
        FROM atoms a
        LEFT JOIN sources s ON s.id = a.source_id
        WHERE a.agent_id = $2
          AND a.is_active = true
          AND ($3::text[] IS NULL OR a.domain_tags && $3)
          AND ($5::text[] IS NULL OR a.atom_type = ANY($5))
          AND ($6::timestamptz IS NULL OR COALESCE(a.remembered_on, a.created_at) >= $6)
          AND ($7::timestamptz IS NULL OR COALESCE(a.remembered_on, a.created_at) < $7)
        ORDER BY a.embedding <=> $1::vector ASC
        LIMIT $4
        """,
        embedding,
        agent_id,
        domain_tags,
        over_fetch,
        atom_types,
        since,
        until,
    )

    # Apply similarity floor, confidence filter, compute composite score, sort.
    rows = [r for r in rows if r["cosine_sim"] >= min_similarity]
    if settings.recall_confidence_floor_enabled:
        rows = [r for r in rows if r["confidence_effective"] >= min_confidence]

    # Filter superseded atoms unless requested
    if not include_superseded:
        rows = await _filter_superseded(conn, rows)

    rows = _dedup_results(rows)

    # Compute per-row composite scores, then apply episodic recency demotion:
    # older episodic atoms with a newer near-duplicate in the candidate set
    # have their score multiplied by the demotion factor (Ticket 4b).
    base_scores = {
        r["id"]: composite_score(r["cosine_sim"], r["confidence_effective"], r["source_type"])
        for r in rows
    }
    final_scores = apply_episodic_recency_demotion(rows, base_scores)
    rows.sort(key=lambda r: final_scores[r["id"]], reverse=True)

    primary = rows[:max_results]
    primary_ids = [r["id"] for r in primary]

    # Build primary responses and apply gap threshold before graph expansion.
    # Gap threshold is applied first so that atoms cut from primary are eligible
    # to surface as graph matches (they must not be in exclude_ids).
    primary_responses = [
        _row_to_atom_response(
            r,
            final_scores[r["id"]],
            match_type="vector",
        )
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
    # are not seeds, so they can surface as graph matches if connected to a surviving atom.
    kept_id_list = list(kept_ids) or primary_ids  # fall back to all if threshold cut everything
    if expand_graph and kept_id_list:
        from .graph_service import expand_graph as graph_expand
        expanded_rows = await graph_expand(
            conn=conn,
            agent_id=agent_id,
            seed_ids=kept_id_list,
            depth=expansion_depth,
            scope_filter={"atom_types": atom_types} if atom_types else None,
            exclude_ids=kept_ids,  # only exclude surviving primary atoms from expanded results
            since=since,
            until=until,
        )

        # Score graph atoms as source_score * edge_weight * discount. Guarantees
        # graph atoms never outrank their source (discount < 1, edge_weight ≤ 1).
        # Uses provenance (via_id, edge_weight) returned by expand_graph, not the
        # atom's own query-similarity — that's the point of graph expansion.
        source_scores = {a["id"]: a["relevance_score"] for a in primary_responses}
        discount = settings.graph_recall_edge_discount
        scored: list[tuple[asyncpg.Record, float]] = []
        for r in expanded_rows:
            via_id = r["via_id"]
            edge_weight = r["edge_weight"] or 1.0
            source_score = source_scores.get(via_id, 0.0)
            score = source_score * edge_weight * discount
            if score > 0:
                scored.append((r, score))
        scored.sort(key=lambda x: x[1], reverse=True)

        # Apply ceiling: graph expansion adds on top of top-k, capped at
        # (multiplier × max_results) atoms.
        ceiling = max_results * settings.graph_recall_expansion_ceiling_multiplier
        scored = scored[:ceiling]

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

        expanded_responses = [
            _row_to_atom_response(r, score, match_type="graph", via=r["via_id"])
            for r, score in scored
        ]

    # Apply token budget — vector matches first, graph matches take any remainder.
    primary_responses, remaining_budget = _apply_token_budget(primary_responses, max_total_tokens)
    expanded_responses, _ = _apply_token_budget(expanded_responses, remaining_budget)

    # Apply verbosity — compress per-atom text
    primary_responses = _apply_verbosity(primary_responses, verbosity, max_content_chars)
    expanded_responses = _apply_verbosity(expanded_responses, verbosity, max_content_chars)

    # Unified response: vector matches first, then graph expansions. Both carry
    # match_type so the caller can distinguish without needing separate lists.
    all_atoms = primary_responses + expanded_responses
    # Graph expansion follows 'related'/'summarises'/etc. edges and can re-
    # introduce atoms that are the target of an active supersedes edge. The
    # primary-rows filter at line 814 didn't catch those. Apply once more
    # post-merge so superseded atoms can't surface via either path.
    if not include_superseded:
        all_atoms = await _filter_superseded(conn, all_atoms)
    await _attach_lifecycle_edges(conn, all_atoms)
    return {
        "atoms": all_atoms,
        "total_retrieved": len(all_atoms),
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


async def _attach_lifecycle_edges(
    conn: asyncpg.Connection,
    atoms: list[dict],
) -> None:
    """Mutates `atoms` in-place: each atom gains a `lifecycle_edges` list with
    any tension_with / narrows edges connecting it to other atoms. supersedes
    is intentionally excluded — those targets are filtered upstream by
    _filter_superseded and we don't surface the relationship metadata."""
    if not atoms:
        return
    atom_ids = [a["id"] for a in atoms]
    rows = await conn.fetch(
        """
        SELECT e.source_id, e.target_id, e.edge_type, e.weight,
               e.metadata->>'reasoning' AS reasoning
        FROM edges e
        JOIN atoms src ON src.id = e.source_id
        JOIN atoms tgt ON tgt.id = e.target_id
        WHERE e.edge_type IN ('tension_with', 'narrows')
          AND src.is_active = true
          AND tgt.is_active = true
          AND (e.source_id = ANY($1) OR e.target_id = ANY($1))
        """,
        atom_ids,
    )
    by_atom: dict = {aid: [] for aid in atom_ids}
    seen: set = set()
    for r in rows:
        src, tgt, et = r["source_id"], r["target_id"], r["edge_type"]
        # surface the edge on whichever endpoint is in the result set.
        # de-dup on (this_atom, related_atom, edge_type).
        if src in by_atom:
            key = (src, tgt, et)
            if key not in seen:
                seen.add(key)
                by_atom[src].append({
                    "related_atom_id": tgt,
                    "relationship": et,
                    "reasoning": r["reasoning"],
                    "weight": float(r["weight"]),
                })
        if tgt in by_atom:
            key = (tgt, src, et)
            if key not in seen:
                seen.add(key)
                by_atom[tgt].append({
                    "related_atom_id": src,
                    "relationship": et,
                    "reasoning": r["reasoning"],
                    "weight": float(r["weight"]),
                })
    for atom in atoms:
        atom["lifecycle_edges"] = by_atom.get(atom["id"], [])


async def get_atom(
    conn: asyncpg.Connection,
    agent_id: UUID,
    atom_id: UUID,
) -> dict | None:
    row = await _get_atom_row(conn, atom_id, agent_id)
    if row is None:
        return None
    return _row_to_atom_response(row)


async def recent(
    conn: asyncpg.Connection,
    agent_id: UUID,
    limit: int = 20,
    since: datetime | None = None,
    until: datetime | None = None,
    atom_types: list[str] | None = None,
    domain_tags: list[str] | None = None,
    include_superseded: bool = False,
    order_by: str = "stored",
) -> list[dict]:
    """Newest active atoms. No embedding, no access-count update.

    order_by="stored": created_at DESC (default). "event":
    COALESCE(remembered_on, created_at) DESC. All filters are in the WHERE
    clause, before LIMIT."""
    order = (
        "COALESCE(a.remembered_on, a.created_at) DESC, a.created_at DESC"
        if order_by == "event" else "a.created_at DESC"
    )
    rows = await conn.fetch(
        f"""
        SELECT
            a.id, a.agent_id, a.atom_type, a.text_content, a.structured,
            a.confidence_alpha, a.confidence_beta,
            a.source_type, a.domain_tags, a.created_at, a.last_accessed, a.access_count, a.is_active,
            a.remembered_on, a.source_id, s.origin_class,
            effective_confidence(
                a.confidence_alpha, a.confidence_beta,
                a.decay_type, a.decay_half_life_days,
                a.created_at, a.last_accessed, a.access_count
            ) AS confidence_effective
        FROM atoms a
        LEFT JOIN sources s ON s.id = a.source_id
        WHERE a.agent_id = $1
          AND a.is_active = true
          AND ($2::text[] IS NULL OR a.atom_type = ANY($2))
          AND ($3::text[] IS NULL OR a.domain_tags && $3)
          AND ($4::timestamptz IS NULL OR COALESCE(a.remembered_on, a.created_at) >= $4)
          AND ($5::timestamptz IS NULL OR COALESCE(a.remembered_on, a.created_at) < $5)
          AND ($6 OR NOT EXISTS (
                SELECT 1 FROM edges e JOIN atoms src ON src.id = e.source_id
                WHERE e.target_id = a.id AND e.edge_type = 'supersedes' AND src.is_active = true))
        ORDER BY {order}
        LIMIT $7
        """,
        agent_id, atom_types, domain_tags, since, until, include_superseded, limit,
    )
    return [_row_to_atom_response(r) for r in rows]


async def soft_delete_atom(
    conn: asyncpg.Connection,
    agent_id: UUID,
    atom_id: UUID,
) -> bool:
    result = await conn.execute(
        """
        UPDATE atoms SET is_active = false, deactivation_reason = 'deleted_by_agent'
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
    metadata: dict | None = None,
) -> dict | None:
    row = await conn.fetchrow(
        """
        INSERT INTO edges (source_id, target_id, edge_type, weight, metadata)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
        RETURNING id, source_id, target_id, edge_type, weight
        """,
        source_id,
        target_id,
        edge_type,
        weight,
        json.dumps(metadata) if metadata is not None else None,
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

    # ── Cold-start enrichment ──

    # Topics: top domain tags by frequency
    tag_rows = await conn.fetch(
        """
        SELECT unnest(domain_tags) AS tag, COUNT(*) AS cnt
        FROM atoms
        WHERE agent_id = $1 AND is_active = true AND domain_tags != '{}'
        GROUP BY tag
        ORDER BY cnt DESC
        LIMIT 8
        """,
        agent_id,
    )
    topics = [r["tag"] for r in tag_rows]

    # Date range
    date_row = await conn.fetchrow(
        """
        SELECT MIN(created_at)::date AS earliest, MAX(created_at)::date AS latest
        FROM atoms
        WHERE agent_id = $1 AND is_active = true
        """,
        agent_id,
    )
    date_range = None
    if date_row and date_row["earliest"] is not None:
        date_range = {
            "earliest": str(date_row["earliest"]),
            "latest": str(date_row["latest"]),
        }

    # Most accessed (top 3)
    accessed_rows = await conn.fetch(
        """
        SELECT text_content, access_count
        FROM atoms
        WHERE agent_id = $1 AND is_active = true AND access_count > 0
        ORDER BY access_count DESC
        LIMIT 3
        """,
        agent_id,
    )
    most_accessed = [
        {"text": r["text_content"][:60], "hits": r["access_count"]}
        for r in accessed_rows
    ]

    return {
        "agent_id": agent_id,
        "total_atoms": row["total_atoms"],
        "active_atoms": row["active_atoms"],
        "atoms_by_type": {
            "episodic": row["episodic"],
            "semantic": row["semantic"],
            "procedural": row["procedural"],
        },
        "arc_atoms": row["arc_atoms"],
        "total_edges": edge_count,
        "avg_effective_confidence": float(row["avg_effective_confidence"]),
        "active_views": view_count,
        "granted_capabilities": granted_count,
        "received_capabilities": received_count,
        "topics": topics,
        "date_range": date_range,
        "most_accessed": most_accessed,
    }
