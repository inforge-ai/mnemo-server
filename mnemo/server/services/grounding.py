"""Deterministic post-decomposition checks (docs/phase1-design.md §5).

Pure functions. Results go to atoms.verification and the response `flags`.
Only `ungrounded_quote` changes anything (one confidence band lower);
`abbreviation_altered`, `unsupported_term` and `no_subject` are record-only.
The store never expands or interprets an abbreviation.
"""

from __future__ import annotations

import re
import unicodedata

CHECKS_VERSION = "v1"

UNGROUNDED_QUOTE = "ungrounded_quote"
ABBREVIATION_ALTERED = "abbreviation_altered"
UNSUPPORTED_TERM = "unsupported_term"
NO_SUBJECT = "no_subject"

# Flags that lower the stored confidence band. Everything else is a signal.
BAND_LOWERING = frozenset({UNGROUNDED_QUOTE})

ABBREVIATION_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,5}\b")

_TRANSLATE = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    " ": " ",
})
_WS_RE = re.compile(r"\s+")

_PRONOUNS = frozenset({
    "i", "me", "my", "you", "your", "he", "him", "his", "she", "her", "it", "its",
    "we", "our", "they", "them", "their", "this", "that", "these", "those",
})
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def normalise(text: str) -> str:
    """NFKC, ASCII quotes/dashes, casefold, collapsed whitespace, trimmed."""
    t = unicodedata.normalize("NFKC", text or "").translate(_TRANSLATE).casefold()
    return _WS_RE.sub(" ", t).strip()


def _abbreviations(text: str) -> set[str]:
    return set(ABBREVIATION_RE.findall(text or ""))


def quote_grounded(quote: str, source: str) -> bool:
    q = normalise(quote)
    return bool(q) and q in normalise(source)


def abbreviation_altered(quote: str, atom_text: str) -> bool:
    """An abbreviation in the quote is missing (exact, whole word) from the atom."""
    return bool(_abbreviations(quote) - _abbreviations(atom_text))


def unsupported_term(atom_text: str, source: str) -> bool:
    """An abbreviation-shaped term in the atom does not occur in the source."""
    return bool(_abbreviations(atom_text) - _abbreviations(source))


def no_subject(atom_text: str, known_names: tuple[str, ...] = ()) -> bool:
    """Simple heuristic (record-only): the atom starts with a pronoun or
    demonstrative ("She moved…", "This is…"), or starts with a lowercase
    word, which in practice means a bare verb phrase with the subject
    dropped ("went camping in July"). A leading number or code is fine, and
    so is a known name in any case (agent names are often lowercase: "clio")."""
    stripped = (atom_text or "").lstrip(" \t\n\"'([{-*•")
    if not stripped:
        return True
    if stripped[0].isdigit():
        return False
    low = stripped.casefold()
    for name in known_names:
        n = (name or "").strip().casefold()
        if n and low.startswith(n) and (len(low) == len(n) or not low[len(n)].isalnum()):
            return False
    words = _WORD_RE.findall(stripped)
    if not words:
        return True
    first = words[0]
    return first.casefold() in _PRONOUNS or first[0].islower()


def run_checks(
    atom_text: str, source_quote: str | None, source_text: str, known_names: tuple[str, ...] = (),
) -> list[str]:
    """Return the flags for one atom, in a stable order. `known_names`:
    speaker, addressee, agent and user names from the request."""
    quote = source_quote or ""
    flags: list[str] = []
    if not quote_grounded(quote, source_text):
        flags.append(UNGROUNDED_QUOTE)
    if abbreviation_altered(quote, atom_text):
        flags.append(ABBREVIATION_ALTERED)
    if unsupported_term(atom_text, source_text):
        flags.append(UNSUPPORTED_TERM)
    if no_subject(atom_text, known_names):
        flags.append(NO_SUBJECT)
    return flags


def lowers_band(flags: list[str]) -> bool:
    return any(f in BAND_LOWERING for f in flags)


def verification_record(flags: list[str], band_downgraded: bool) -> dict:
    return {
        "checks": CHECKS_VERSION,
        "flags": flags,
        "quote_found": UNGROUNDED_QUOTE not in flags,
        "band_downgraded": band_downgraded,
    }
