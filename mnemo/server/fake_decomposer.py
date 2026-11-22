"""Scripted decomposer for the test suite (MNEMO_DECOMPOSER_PROVIDER=fake).

Tests queue results with `script(...)`; each decompose call pops the next
one. A queued Exception instance is raised instead of returned. With nothing
queued, the deterministic regex decomposer is used. No network, no model.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from .decomposer import DecomposedAtom, decompose as regex_decompose

_queue: deque = deque()
calls: list[dict] = []


def script(*results) -> None:
    """Queue results: each is a list[DecomposedAtom] or an Exception."""
    _queue.extend(results)


def reset() -> None:
    _queue.clear()
    calls.clear()


async def fake_decompose(
    text: str,
    remembered_on: datetime | None = None,
    domain_tags: list[str] | None = None,
):
    from .llm_decomposer import DecomposerResult

    calls.append({"text": text, "remembered_on": remembered_on, "domain_tags": domain_tags})
    if _queue:
        item = _queue.popleft()
        if isinstance(item, BaseException):
            raise item
        atoms: list[DecomposedAtom] = list(item)
        return DecomposerResult(atoms=atoms)
    return DecomposerResult(atoms=regex_decompose(text, domain_tags))
