"""Decomposer v2 interface (docs/phase1-design.md §4)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

PROMPT_VERSION = "prompt_v2"

AtomType = Literal["episodic", "semantic", "procedural"]


class DecomposerError(Exception):
    """Decomposition failed. `reason` is a fixed-vocabulary, plain message
    that is safe to return to the caller: never source text or model output."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class DecomposeRequest:
    text: str
    agent_name: str
    speaker: str
    addressee: str | None = None
    channel: str | None = None
    remembered_on: datetime | None = None   # request date; None → today (UTC)
    user_name: str | None = None            # agents.metadata.user_name
    part: tuple[int, int] | None = None     # (k, n) when the note was split


@dataclass
class CandidateAtom:
    text: str
    atom_type: AtomType
    confidence: float
    source_quote: str
    remembered_on: datetime | None = None
    subject: str | None = None
    entity_resolved: bool = True
    source_type: str = "direct_experience"
    # Providers that assign Beta parameters directly (regex) set this;
    # otherwise `confidence` is mapped through confidence_to_beta.
    beta: tuple[float, float] | None = None
    # prompt_v3: the atom's own date and how precise it is (docs/phase2-design.md §4).
    event_date: datetime | None = None
    date_precision: str = "unknown"
    # prompt_v3 gives no confidence: the store uses the origin_class prior.
    confidence_from_prior: bool = False


@dataclass
class DecomposeResult:
    atoms: list[CandidateAtom]
    usage: list[dict] = field(default_factory=list)   # one entry per model call
    warnings: list[str] = field(default_factory=list)
    version: str = ""                                  # provider:model:prompt_version


class Decomposer(Protocol):
    provider: str
    model: str

    async def decompose(self, req: DecomposeRequest) -> DecomposeResult: ...


def version_string(provider: str, model: str, prompt_version: str | None = None) -> str:
    return f"{provider}:{model}:{prompt_version or prompt_module().PROMPT_VERSION}"


def prompt_module():
    """The decomposer prompt in use: settings.decomposer_prompt (v3 from Phase 2; v2 for rollback)."""
    from ..config import settings
    if settings.decomposer_prompt == "v2":
        from . import prompt_v2
        return prompt_v2
    from . import prompt_v3
    return prompt_v3


# ── Post-processing shared by every provider ────────────────────────────────

def confidence_to_beta(confidence: float) -> tuple[float, float]:
    """Map confidence [0,1] to Beta parameters (bands unchanged from v1)."""
    if confidence >= 0.8:
        return (8.0, 1.0)
    elif confidence >= 0.6:
        return (4.0, 2.0)
    elif confidence >= 0.4:
        return (3.0, 2.0)
    elif confidence >= 0.25:
        return (2.0, 3.0)
    else:
        return (2.0, 4.0)


BETA_LADDER: tuple[tuple[float, float], ...] = ((8.0, 1.0), (4.0, 2.0), (3.0, 2.0), (2.0, 3.0), (2.0, 4.0))


def lower_one_band(alpha: float, beta: float) -> tuple[float, float]:
    """Next band down the ladder; the lowest band is the floor."""
    try:
        i = BETA_LADDER.index((alpha, beta))
    except ValueError:
        return (alpha, beta)
    return BETA_LADDER[min(i + 1, len(BETA_LADDER) - 1)]


# Narrow backstop for the LLM tagging a time-scoped state / plan claim as
# semantic. Prefer false negatives; a broader sweep belongs in the prompt.
_STATE_CLAIM_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bis\s+(?:planned|scheduled|being\s+planned|in\s+progress)\b", re.IGNORECASE),
    re.compile(r"\bis\s+(?:currently|now|the\s+current|the\s+active|the\s+next)\b", re.IGNORECASE),
    re.compile(r"\b(?:has|have)\s+not\s+(?:yet\s+)?been\b", re.IGNORECASE),
    re.compile(r"\bis\s+on\s+the\s+roadmap\b", re.IGNORECASE),
    re.compile(r"\bare\s+planning\s+to\b", re.IGNORECASE),
)


def looks_like_state_claim(text: str) -> bool:
    return any(p.search(text) for p in _STATE_CLAIM_PATTERNS)


def parse_date(value) -> datetime | None:
    """ISO date or datetime → datetime; None for null/empty/unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


# ── Parsing structured model output ─────────────────────────────────────────

def parse_items(items) -> tuple[list[CandidateAtom], list[str]]:
    """Validate record_atoms items. Malformed items are skipped with a
    warning; the rest of the batch is kept."""
    atoms: list[CandidateAtom] = []
    skipped = 0
    if not isinstance(items, list):
        raise DecomposerError("malformed tool output")
    for item in items:
        try:
            if not isinstance(item, dict):
                raise ValueError
            text = item["text"]
            quote = item["source_quote"]
            atom_type = item["type"]
            from_prior = "confidence" not in item
            confidence = 0.0 if from_prior else float(item["confidence"])
            if not isinstance(text, str) or not text.strip() or not isinstance(quote, str):
                raise ValueError
            if atom_type not in ("episodic", "semantic", "procedural"):
                raise ValueError
            subject = item.get("subject")
            event_date = parse_date(item.get("event_date"))
            precision = item.get("date_precision")
            if event_date is None:
                precision = "unknown"
            elif precision not in ("day", "month", "year"):
                precision = "day"
            atoms.append(CandidateAtom(
                text=text.strip(),
                atom_type=atom_type,
                confidence=min(max(confidence, 0.0), 1.0),
                source_quote=quote,
                remembered_on=event_date or parse_date(item.get("remembered_on")),
                event_date=event_date,
                date_precision=precision,
                confidence_from_prior=from_prior,
                subject=subject.strip() if isinstance(subject, str) and subject.strip() else None,
                entity_resolved=item.get("entity_resolved", True) is not False,
            ))
        except (KeyError, TypeError, ValueError):
            skipped += 1
    warnings = [f"skipped {skipped} malformed atom(s) from the decomposer"] if skipped else []
    return atoms, warnings
