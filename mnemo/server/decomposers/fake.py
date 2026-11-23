"""Scripted decomposer for the test suite (MNEMO_DECOMPOSER_PROVIDER=fake).

Tests queue results with `script(...)`; each decompose call pops the next.
An item may be: a list of CandidateAtom / DecomposedAtom, a DecomposeResult,
an Exception instance (raised), or an async callable taking the request
(awaited; its return value is treated the same way). With nothing queued the
deterministic regex decomposer is used. No network, no model.
"""

from __future__ import annotations

from collections import deque

from ..decomposer import DecomposedAtom
from .base import CandidateAtom, DecomposeRequest, DecomposeResult, version_string
from .regex import from_regex

_queue: deque = deque()
calls: list[DecomposeRequest] = []


def script(*results) -> None:
    _queue.extend(results)


def reset() -> None:
    _queue.clear()
    calls.clear()


def _convert(atom) -> CandidateAtom:
    if isinstance(atom, CandidateAtom):
        return atom
    if isinstance(atom, DecomposedAtom):
        return CandidateAtom(
            text=atom.text, atom_type=atom.atom_type, confidence=0.0, source_quote=atom.text,
            remembered_on=atom.remembered_on, source_type=atom.source_type,
            beta=(atom.confidence_alpha, atom.confidence_beta),
        )
    raise TypeError(f"cannot script {type(atom).__name__}")


class FakeDecomposer:
    provider = "fake"
    model = "scripted"

    async def decompose(self, req: DecomposeRequest) -> DecomposeResult:
        calls.append(req)
        version = version_string(self.provider, self.model)
        if not _queue:
            return DecomposeResult(atoms=from_regex(req.text), version=version)
        item = _queue.popleft()
        if callable(item) and not isinstance(item, BaseException):
            item = await item(req)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, DecomposeResult):
            item.version = item.version or version
            return item
        return DecomposeResult(atoms=[_convert(a) for a in item], version=version)
