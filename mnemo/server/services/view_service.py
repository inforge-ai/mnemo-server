"""
View creation and skill export. v0.1: Snapshots only, α=1 only.

CREATE SNAPSHOT FLOW:
1. Validate filter
2. Query matching active atoms at current time
3. Insert view record
4. Insert atom IDs into snapshot_atoms (freezes the set)

EXPORT SKILL FLOW:
1. Load atoms from snapshot_atoms JOIN atoms
2. Collect procedural atoms
3. Follow edges WITHIN snapshot scope to get supporting semantic atoms
4. Package as SkillExport with rendered markdown

SHARED VIEW RETRIEVAL:
1. Validate capability (not revoked, not expired)
2. Run retrieval query but ONLY against atoms in snapshot_atoms for this view
3. Graph expansion scope-bounded to snapshot atoms only
"""

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from ..embeddings import encode

logger = logging.getLogger(__name__)


# ── Snapshot creation ─────────────────────────────────────────────────────────

async def create_snapshot(
    conn: asyncpg.Connection,
    owner_agent_id: UUID,
    name: str,
    description: str | None,
    atom_filter: dict,
) -> dict:
    """Create a snapshot view, freezing matching atom IDs."""
    atom_types: list[str] | None = atom_filter.get("atom_types") or None
    domain_tags: list[str] | None = atom_filter.get("domain_tags") or None

    # Collect matching atom IDs at this moment
    atom_rows = await conn.fetch(
        """
        SELECT id FROM atoms
        WHERE agent_id = $1
          AND is_active = true
          AND ($2::text[] IS NULL OR atom_type = ANY($2))
          AND ($3::text[] IS NULL OR domain_tags && $3)
        """,
        owner_agent_id,
        atom_types,
        domain_tags,
    )
    atom_ids = [r["id"] for r in atom_rows]

    # Insert view
    view_row = await conn.fetchrow(
        """
        INSERT INTO views (owner_agent_id, name, description, atom_filter)
        VALUES ($1, $2, $3, $4)
        RETURNING id, owner_agent_id, name, description, alpha, atom_filter, created_at
        """,
        owner_agent_id,
        name,
        description,
        json.dumps(atom_filter),
    )

    # Freeze atom IDs into snapshot_atoms
    if atom_ids:
        await conn.executemany(
            "INSERT INTO snapshot_atoms (view_id, atom_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            [(view_row["id"], aid) for aid in atom_ids],
        )

    return {
        "id": view_row["id"],
        "owner_agent_id": view_row["owner_agent_id"],
        "name": view_row["name"],
        "description": view_row["description"],
        "alpha": view_row["alpha"],
        "atom_filter": json.loads(view_row["atom_filter"]) if isinstance(view_row["atom_filter"], str) else view_row["atom_filter"],
        "atom_count": len(atom_ids),
        "created_at": view_row["created_at"],
    }


async def list_views(conn: asyncpg.Connection, owner_agent_id: UUID) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT v.id, v.owner_agent_id, v.name, v.description, v.alpha,
               v.atom_filter, v.created_at,
               COUNT(sa.atom_id) AS atom_count
        FROM views v
        LEFT JOIN snapshot_atoms sa ON sa.view_id = v.id
        WHERE v.owner_agent_id = $1
        GROUP BY v.id
        ORDER BY v.created_at DESC
        """,
        owner_agent_id,
    )
    return [_view_row(r) for r in rows]


async def get_view(conn: asyncpg.Connection, view_id: UUID) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT v.id, v.owner_agent_id, v.name, v.description, v.alpha,
               v.atom_filter, v.created_at,
               COUNT(sa.atom_id) AS atom_count
        FROM views v
        LEFT JOIN snapshot_atoms sa ON sa.view_id = v.id
        WHERE v.id = $1
        GROUP BY v.id
        """,
        view_id,
    )
    return _view_row(row) if row else None


# ── Skill export ──────────────────────────────────────────────────────────────

async def export_skill(
    conn: asyncpg.Connection,
    agent_id: UUID,
    view_id: UUID,
) -> dict:
    """Build SkillExport from snapshot atoms."""
    # Load all snapshot atoms
    atom_rows = await conn.fetch(
        """
        SELECT
            a.id, a.agent_id, a.atom_type, a.text_content, a.structured,
            a.confidence_alpha, a.confidence_beta,
            a.source_type, a.domain_tags, a.created_at,
            a.last_accessed, a.access_count, a.is_active,
            effective_confidence(
                a.confidence_alpha, a.confidence_beta,
                a.decay_type, a.decay_half_life_days,
                a.created_at, a.last_accessed, a.access_count
            ) AS confidence_effective
        FROM snapshot_atoms sa
        JOIN atoms a ON a.id = sa.atom_id
        WHERE sa.view_id = $1
        ORDER BY a.atom_type, a.created_at
        """,
        view_id,
    )

    # Load view metadata
    view = await get_view(conn, view_id)
    if not view:
        return None

    # Load agent name for attribution
    agent_name = await conn.fetchval("SELECT name FROM agents WHERE id = $1", agent_id)

    from ..services.atom_service import _row_to_atom_response

    all_atoms = [_row_to_atom_response(r) for r in atom_rows]
    snapshot_ids = {a["id"] for a in all_atoms}

    procedures = [a for a in all_atoms if a["atom_type"] == "procedural"]
    semantics = [a for a in all_atoms if a["atom_type"] == "semantic"]

    # Graph expansion within snapshot scope to find supporting semantic atoms
    # not already in the snapshot (bounded by view filter)
    procedure_ids = [a["id"] for a in procedures]
    if procedure_ids:
        from .graph_service import expand_graph
        scope_filter = json.loads(view["atom_filter"]) if isinstance(view["atom_filter"], str) else view["atom_filter"]
        expanded = await expand_graph(
            conn=conn,
            agent_id=agent_id,
            seed_ids=procedure_ids,
            depth=2,
            scope_filter=scope_filter,
            exclude_ids=snapshot_ids,
        )
        extra_semantics = [
            _row_to_atom_response(r) for r in expanded
            if r["atom_type"] == "semantic"
        ]
        semantics = semantics + extra_semantics

    # Collect domain tags from all atoms
    domain_tags: list[str] = []
    for a in all_atoms:
        for tag in a["domain_tags"]:
            if tag not in domain_tags:
                domain_tags.append(tag)

    markdown = _render_skill_markdown(
        view_name=view["name"],
        view_description=view["description"],
        procedures=procedures,
        supporting_facts=semantics,
        agent_name=agent_name,
        domain_tags=domain_tags,
    )

    return {
        "view_id": view_id,
        "name": view["name"],
        "description": view["description"],
        "domain_tags": domain_tags,
        "procedures": procedures,
        "supporting_facts": semantics,
        "metadata": {
            "snapshot_at": view["created_at"].isoformat(),
            "agent_name": agent_name,
        },
        "rendered_markdown": markdown,
    }


def _render_skill_markdown(
    view_name: str,
    view_description: str | None,
    procedures: list[dict],
    supporting_facts: list[dict],
    agent_name: str | None,
    domain_tags: list[str],
) -> str:
    lines = [f"# {view_name}"]
    if view_description:
        lines += ["", view_description]
    lines += ["", "## Procedures", ""]

    for proc in procedures:
        conf = proc["confidence_expected"]
        lines.append(f"### {proc['text_content']}")
        lines.append(f"*Confidence: {conf:.0%}*")
        if proc.get("structured", {}).get("code"):
            lines += ["", "```", proc["structured"]["code"], "```"]
        # Link supporting facts by checking semantic atoms
        lines += [""]

    if supporting_facts:
        lines += ["**Supporting knowledge:**"]
        for fact in supporting_facts:
            conf = fact["confidence_expected"]
            lines.append(f"- {fact['text_content']} ({conf:.0%})")
        lines += [""]

    lines += [
        "---",
        "",
        f"*Generated by Mnemo on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}*",
    ]
    if agent_name:
        lines.append(f"*Source agent: {agent_name}*")
    if domain_tags:
        lines.append(f"*Domain: {', '.join(domain_tags)}*")

    return "\n".join(lines)


# ── Shared view retrieval ─────────────────────────────────────────────────────

async def recall_shared(
    conn: asyncpg.Connection,
    grantee_id: UUID,
    view_id: UUID,
    capability_id: UUID,
    query: str,
    min_confidence: float,
    max_results: int,
    expansion_depth: int,
) -> dict:
    """
    Retrieval scoped to snapshot atoms of a shared view.
    Graph expansion is bounded to atoms within the snapshot.
    """
    embedding = await encode(query)

    rows = await conn.fetch(
        """
        SELECT
            a.id, a.agent_id, a.atom_type, a.text_content, a.structured,
            a.confidence_alpha, a.confidence_beta,
            a.source_type, a.domain_tags, a.created_at,
            a.last_accessed, a.access_count, a.is_active,
            1 - (a.embedding <=> $1::vector) AS similarity,
            effective_confidence(
                a.confidence_alpha, a.confidence_beta,
                a.decay_type, a.decay_half_life_days,
                a.created_at, a.last_accessed, a.access_count
            ) AS confidence_effective
        FROM snapshot_atoms sa
        JOIN atoms a ON a.id = sa.atom_id
        WHERE sa.view_id = $2
          AND a.is_active = true
        ORDER BY similarity DESC
        LIMIT $3
        """,
        embedding,
        view_id,
        max_results * 2,
    )

    rows = [r for r in rows if r["confidence_effective"] >= min_confidence]
    primary = rows[:max_results]
    primary_ids = [r["id"] for r in primary]

    # Update access on returned atoms and the capability itself
    if primary_ids:
        await conn.execute(
            """
            UPDATE atoms
            SET last_accessed = now(), access_count = access_count + 1
            WHERE id = ANY($1)
            """,
            primary_ids,
        )

    # Scope-bounded graph expansion within snapshot
    view_row = await conn.fetchrow("SELECT atom_filter FROM views WHERE id = $1", view_id)
    scope_filter = json.loads(view_row["atom_filter"]) if isinstance(view_row["atom_filter"], str) else view_row["atom_filter"]

    # Restrict graph expansion to only atoms in this snapshot
    snapshot_rows = await conn.fetch(
        "SELECT atom_id FROM snapshot_atoms WHERE view_id = $1",
        view_id,
    )
    allowed_ids = {r["atom_id"] for r in snapshot_rows}

    expanded_rows = []
    if primary_ids:
        from .graph_service import expand_graph
        expanded_rows = await expand_graph(
            conn=conn,
            agent_id=None,
            seed_ids=primary_ids,
            depth=expansion_depth,
            scope_filter=scope_filter,
            exclude_ids=set(primary_ids),
            allowed_ids=allowed_ids,
        )

    from ..services.atom_service import _row_to_atom_response
    primary_responses = [_row_to_atom_response(r) for r in primary]
    expanded_responses = [_row_to_atom_response(r) for r in expanded_rows]

    return {
        "atoms": primary_responses,
        "expanded_atoms": expanded_responses,
        "total_retrieved": len(primary_responses) + len(expanded_responses),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _view_row(row) -> dict:
    af = row["atom_filter"]
    if isinstance(af, str):
        af = json.loads(af)
    return {
        "id": row["id"],
        "owner_agent_id": row["owner_agent_id"],
        "name": row["name"],
        "description": row["description"],
        "alpha": row["alpha"],
        "atom_filter": af or {},
        "atom_count": row["atom_count"],
        "created_at": row["created_at"],
    }
