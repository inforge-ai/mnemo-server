"""Decomposer v2: provider selection, splitting, deadline, post-processing.

See docs/phase1-design.md §4. Entry point: `decompose_note(req)`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from ..config import settings
from .base import (
    CandidateAtom, DecomposeRequest, DecomposeResult, Decomposer, DecomposerError,
    confidence_to_beta, looks_like_state_claim,
)
from .split import MAX_PART_CHARS, split_text

logger = logging.getLogger(__name__)

MAX_PARALLEL_PARTS = 4

__all__ = [
    "CandidateAtom", "DecomposeRequest", "DecomposeResult", "DecomposerError",
    "decompose_note", "get_decomposer", "beta_for",
]


def get_decomposer() -> Decomposer:
    provider = settings.decomposer_provider
    if provider == "anthropic":
        from .anthropic import AnthropicDecomposer
        return AnthropicDecomposer(settings.decomposer_model)
    if provider == "openrouter":
        from .openrouter import OpenRouterDecomposer
        return OpenRouterDecomposer(settings.decomposer_model)
    if provider == "regex":
        from .regex import RegexDecomposer
        return RegexDecomposer()
    if provider == "fake":
        from .fake import FakeDecomposer
        return FakeDecomposer()
    raise DecomposerError(f"unknown decomposer provider {provider!r}")


def beta_for(atom: CandidateAtom) -> tuple[float, float]:
    """Beta parameters before any check-based downgrade. An unresolved
    definite-article reference lowers confidence by 0.2 (unchanged from v1)."""
    if atom.beta is not None:
        return atom.beta
    confidence = atom.confidence
    if not atom.entity_resolved:
        confidence = max(0.0, confidence - 0.2)
    return confidence_to_beta(confidence)


def _post_process(atoms: list[CandidateAtom], req: DecomposeRequest) -> list[CandidateAtom]:
    for a in atoms:
        if a.atom_type == "semantic" and looks_like_state_claim(a.text):
            a.atom_type = "episodic"
        if a.remembered_on is None:
            a.remembered_on = req.remembered_on
    return atoms


async def _run(decomposer: Decomposer, req: DecomposeRequest) -> DecomposeResult:
    parts = split_text(req.text, MAX_PART_CHARS)
    if len(parts) == 1:
        result = await decomposer.decompose(req)
    else:
        sem = asyncio.Semaphore(MAX_PARALLEL_PARTS)

        async def one(k: int, text: str) -> DecomposeResult:
            async with sem:
                return await decomposer.decompose(replace(req, text=text, part=(k, len(parts))))

        # All parts must succeed: any failure fails the whole store.
        results = await asyncio.gather(*(one(k, t) for k, t in enumerate(parts, start=1)))
        result = DecomposeResult(
            atoms=[a for r in results for a in r.atoms],
            usage=[u for r in results for u in r.usage],
            warnings=[f"input split into {len(parts)} parts"] + [w for r in results for w in r.warnings],
            version=results[0].version,
        )
    result.atoms = _post_process(result.atoms, req)
    return result


async def decompose_note(req: DecomposeRequest, decomposer: Decomposer | None = None) -> DecomposeResult:
    """Decompose a note under the total deadline (all parts and attempts).

    Raises DecomposerError with a plain reason on any failure."""
    if not req.text or not req.text.strip():
        return DecomposeResult(atoms=[], version="")
    decomposer = decomposer or get_decomposer()
    try:
        async with asyncio.timeout(settings.decomposer_deadline_seconds):
            return await _run(decomposer, req)
    except TimeoutError as exc:
        raise DecomposerError("decomposer deadline exceeded") from exc
    except DecomposerError:
        raise
    except Exception as exc:
        logger.exception("decomposer raised an unexpected error")
        raise DecomposerError("decomposer error") from exc
