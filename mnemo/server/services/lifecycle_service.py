"""Lifecycle relationship detection (docs/episodic_suppression-tension.md).

Four-way classifier: supersedes / tension_with / narrows / independent.
Three are edge-creating; "independent" is a no-op. Runs from
atom_service.store_background after the store transaction commits, gated
by settings.lifecycle_detection_enabled. Failure mode: log and skip;
permanent failures land in lifecycle_dlq.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from functools import lru_cache
from uuid import UUID

import asyncpg
from anthropic import AsyncAnthropic

from ..config import settings
from . import atom_service

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


# ── LLM classifier ──────────────────────────────────────────────────────────

MODEL = "claude-haiku-4-5-20251001"

LIFECYCLE_SYSTEM_PROMPT = """You are evaluating the relationship between a newly stored memory atom and an existing atom about a similar topic.

Classify the relationship. Respond with JSON only, no prose, no markdown:
{"relationship": "supersedes" | "tension_with" | "narrows" | "independent", "confidence": 0.0-1.0, "reasoning": "<one sentence>"}

Definitions:
- "supersedes": the new atom replaces the existing one. Use this for state changes, corrections, and preference updates where the existing atom is now historically accurate but no longer current. Examples: "X is planned" -> "X is done"; "Tom prefers A" -> "Tom now prefers B"; "Score is 76.1%" -> "Score was actually 82.1%; 76.1% was an earlier result".

- "tension_with": both atoms remain true and active, but together they identify an unresolved discrepancy or anomaly worth surfacing. Use this when the new atom is *evidence against* or *in tension with* the existing one without directly invalidating it. Examples: "Newtonian gravity works" + "Mercury's perihelion precesses anomalously"; "Mnemo achieves 82.1% on LoCoMo" + "Hindsight achieves 91.4% on LongMemEval"; "Strategy X has worked historically" + "Strategy X failed in Q4".

- "narrows": the new atom qualifies or refines the existing one without invalidating it. Both should remain visible together. Examples: "Tom uses Mattermost" -> "Tom uses Zulip for ops, Mattermost for personal"; "Mnemo runs on Postgres" -> "Mnemo runs on Postgres 16 with pgvector".

- "independent": same topic, no logical relationship between them.

Important guardrail:
If the existing atom is a SEMANTIC claim about how the world works (rather than an EPISODIC fact about a state, event, or measurement), strongly prefer "tension_with" over "supersedes" unless the new atom explicitly corrects or invalidates the existing claim with overwhelming evidence. Semantic claims are rarely retired by single new observations; they accumulate evidence and shift through "tension_with" relationships."""

_VALID_RELATIONSHIPS = {"supersedes", "tension_with", "narrows", "independent"}


@lru_cache(maxsize=1)
def _get_client() -> AsyncAnthropic:
    """Singleton Anthropic client. Tests patch this same way as llm_decomposer:
    `patch('mnemo.server.services.lifecycle_service._get_client', ...)`."""
    return AsyncAnthropic()


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()


async def _evaluate_pair(
    new_text: str,
    new_type: str,
    existing_text: str,
    existing_type: str,
    existing_age_days: int,
) -> dict | None:
    """Call Haiku to classify the (existing, new) pair. One retry on transient
    error per spec §4. Returns None on permanent failure."""
    user_prompt = (
        f"EXISTING ATOM (stored {existing_age_days} days ago, type: {existing_type}):\n"
        f'"{existing_text}"\n\n'
        f"NEW ATOM (just stored, type: {new_type}):\n"
        f'"{new_text}"'
    )
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            client = _get_client()
            response = await asyncio.wait_for(
                client.messages.create(
                    model=MODEL,
                    max_tokens=256,
                    system=[{
                        "type": "text",
                        "text": LIFECYCLE_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_prompt}],
                ),
                timeout=settings.lifecycle_llm_timeout_seconds,
            )
            raw = _strip_fences(response.content[0].text)
            parsed = json.loads(raw)
            rel = parsed.get("relationship")
            if rel not in _VALID_RELATIONSHIPS:
                return None
            try:
                confidence = float(parsed.get("confidence", 0.0))
            except (TypeError, ValueError):
                return None
            reasoning = str(parsed.get("reasoning", ""))[:500]  # one-sentence cap
            return {
                "relationship": rel,
                "confidence": confidence,
                "reasoning": reasoning,
                "usage": {
                    "model": response.model,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", None),
                    "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", None),
                },
            }
        except json.JSONDecodeError:
            return None
        except Exception as e:
            last_err = e
            continue
    logger.warning("lifecycle LLM call failed after retry: %s", last_err)
    return None


# ── Orchestrator ─────────────────────────────────────────────────────────────

DETECTOR_VERSION = "auto_lifecycle_v1"

_THRESHOLDS: dict[str, str] = {
    "supersedes": "supersedes_threshold",
    "tension_with": "tension_threshold",
    "narrows": "narrows_threshold",
}


async def _pair_has_lifecycle_edge(
    conn: asyncpg.Connection,
    a_id: UUID,
    b_id: UUID,
) -> bool:
    """Return True if any edge of any lifecycle type connects this pair, in
    either direction. Spec §Edge creation: no competing edges."""
    row = await conn.fetchval(
        """
        SELECT 1 FROM edges
        WHERE edge_type IN ('supersedes', 'tension_with', 'narrows')
          AND (
            (source_id = $1 AND target_id = $2)
            OR (source_id = $2 AND target_id = $1)
          )
        LIMIT 1
        """,
        a_id, b_id,
    )
    return row is not None


async def _record_dlq(
    conn: asyncpg.Connection,
    new_atom_id: UUID,
    candidate_id: UUID | None,
    agent_id: UUID,
    error: str,
) -> None:
    try:
        await conn.execute(
            """
            INSERT INTO lifecycle_dlq (new_atom_id, candidate_id, agent_id, error)
            VALUES ($1, $2, $3, $4)
            """,
            new_atom_id, candidate_id, agent_id, error[:1000],
        )
    except Exception:
        logger.warning("failed to record lifecycle_dlq row", exc_info=True)


async def detect_lifecycle_relationships(
    conn: asyncpg.Connection,
    agent_id: UUID,
    new_atom_id: UUID,
) -> int:
    """For one newly-inserted atom, run candidate query + LLM eval per
    candidate and write the appropriate edge type when the model is confident
    enough. Permanent LLM failures land in lifecycle_dlq.

    Returns the count of edges written by this call. Never raises."""
    new_row = await conn.fetchrow(
        "SELECT id, text_content, atom_type, embedding, created_at FROM atoms WHERE id = $1",
        new_atom_id,
    )
    if new_row is None:
        return 0

    candidates = await _get_candidates(conn, agent_id, new_atom_id, new_row["embedding"])
    if not candidates:
        return 0

    edges_written = 0
    for cand in candidates:
        # Idempotency / no-competing-edges: skip if any lifecycle edge exists
        # for this pair already (either direction). Saves an LLM call too.
        if await _pair_has_lifecycle_edge(conn, new_atom_id, cand["id"]):
            continue

        t0 = time.monotonic()
        existing_age_days = max(
            0,
            int((datetime.now(timezone.utc) - cand["created_at"]).total_seconds() / 86400),
        )
        result = await _evaluate_pair(
            new_text=new_row["text_content"],
            new_type=new_row["atom_type"],
            existing_text=cand["text_content"],
            existing_type=cand["atom_type"],
            existing_age_days=existing_age_days,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        if result is None:
            await _record_dlq(conn, new_atom_id, cand["id"], agent_id, "lifecycle LLM permanent failure")
            logger.warning(
                "lifecycle_check",
                extra={
                    "event": "lifecycle_check",
                    "new_atom_id": str(new_atom_id),
                    "candidate_atom_id": str(cand["id"]),
                    "agent_id": str(agent_id),
                    "cosine": cand["similarity"],
                    "edge_created": False,
                    "latency_ms": latency_ms,
                    "dlq": True,
                },
            )
            continue

        rel = result["relationship"]
        edge_type: str | None = None
        if rel in _THRESHOLDS:
            threshold = getattr(settings, _THRESHOLDS[rel])
            if result["confidence"] >= threshold:
                edge_type = rel

        edge_created = False
        if edge_type is not None:
            try:
                edge = await atom_service.create_edge(
                    conn=conn,
                    source_id=new_atom_id,
                    target_id=cand["id"],
                    edge_type=edge_type,
                    weight=float(result["confidence"]),
                    metadata={
                        "reasoning": result["reasoning"],
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                        "detector": DETECTOR_VERSION,
                        "cosine_at_detection": cand["similarity"],
                    },
                )
                if edge is not None:
                    edges_written += 1
                    edge_created = True
            except Exception:
                logger.warning("lifecycle edge write failed", exc_info=True)

        usage = result.get("usage") or {}
        logger.info(
            "lifecycle_check",
            extra={
                "event": "lifecycle_check",
                "new_atom_id": str(new_atom_id),
                "candidate_atom_id": str(cand["id"]),
                "agent_id": str(agent_id),
                "new_atom_type": new_row["atom_type"],
                "existing_atom_type": cand["atom_type"],
                "cosine": cand["similarity"],
                "llm_relationship": rel,
                "llm_confidence": result["confidence"],
                "llm_reasoning": result.get("reasoning"),
                "edge_created": edge_created,
                "edge_type": edge_type if edge_created else None,
                "latency_ms": latency_ms,
                "haiku_input_tokens": usage.get("input_tokens"),
                "haiku_output_tokens": usage.get("output_tokens"),
            },
        )

    return edges_written
