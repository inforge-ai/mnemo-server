"""Split long notes on paragraph boundaries (docs/phase1-design.md §4)."""

from __future__ import annotations

import re

MAX_PART_CHARS = 8000

_PARA_RE = re.compile(r"\n\s*\n")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _pieces(paragraph: str, limit: int) -> list[str]:
    """A paragraph over the limit: sentences, then whitespace, then hard cut."""
    if len(paragraph) <= limit:
        return [paragraph]
    out: list[str] = []
    for unit in _SENT_RE.split(paragraph):
        while len(unit) > limit:
            cut = unit.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(unit[:cut])
            unit = unit[cut:].lstrip()
        if unit:
            out.append(unit)
    return out


def split_text(text: str, limit: int = MAX_PART_CHARS) -> list[str]:
    """Greedy-pack paragraphs into parts of at most `limit` characters."""
    if len(text) <= limit:
        return [text]
    units: list[tuple[str, str]] = []   # (piece, separator before next)
    for para in _PARA_RE.split(text):
        para = para.strip()
        if not para:
            continue
        pieces = _pieces(para, limit)
        units.extend((p, " ") for p in pieces[:-1])
        units.append((pieces[-1], "\n\n"))
    parts: list[str] = []
    cur = ""
    sep = ""
    for piece, next_sep in units:
        candidate = f"{cur}{sep}{piece}" if cur else piece
        if len(candidate) <= limit:
            cur = candidate
        else:
            parts.append(cur)
            cur = piece
        sep = next_sep
    if cur:
        parts.append(cur)
    return parts
