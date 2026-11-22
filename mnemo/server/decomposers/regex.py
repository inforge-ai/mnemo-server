"""Regex decomposer, only when MNEMO_DECOMPOSER_PROVIDER=regex (no fallback)."""

from __future__ import annotations

from ..decomposer import decompose as regex_decompose
from .base import CandidateAtom, DecomposeRequest, DecomposeResult, version_string


def from_regex(text: str) -> list[CandidateAtom]:
    return [
        CandidateAtom(
            text=a.text, atom_type=a.atom_type, confidence=0.0,
            source_quote=a.text, remembered_on=a.remembered_on,
            source_type=a.source_type, beta=(a.confidence_alpha, a.confidence_beta),
        )
        for a in regex_decompose(text, None)
    ]


class RegexDecomposer:
    provider = "regex"
    model = ""

    async def decompose(self, req: DecomposeRequest) -> DecomposeResult:
        return DecomposeResult(atoms=from_regex(req.text), version=version_string("regex", "", "regex_v1"))
