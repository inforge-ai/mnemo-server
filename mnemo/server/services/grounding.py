"""Deterministic post-decomposition checks (docs/phase1-design.md §5).

Pure functions. Results go to atoms.verification and the response `flags`.
Only `ungrounded_quote` changes anything (one confidence band lower);
`quote_inexact`, `unsupported_term` and `no_subject` are record-only.

Quote grounding (v2) compares WORD sequences, ignoring case, punctuation,
markdown and line breaks:
  exact    the quote's words occur contiguously in the source → no flag;
  inexact  best-aligned source window: >= 90% of the quote's words in order
           and <= 3 characters different → `quote_inexact` (record-only);
  else     `ungrounded_quote` (band lowered). (`abbreviation_altered`
was removed after G2 review: it fired whenever a quote spanned two facts.)
The store never expands or interprets an abbreviation.
"""

from __future__ import annotations

import re
import unicodedata

CHECKS_VERSION = "v2"

UNGROUNDED_QUOTE = "ungrounded_quote"
QUOTE_INEXACT = "quote_inexact"
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
_TOKEN_RE = re.compile(r"[^\W_]+")   # letters/digits; punctuation, markdown and line breaks drop out

INEXACT_MIN_WORD_SHARE = 0.90
INEXACT_MAX_CHAR_DIFF = 3


def normalise(text: str) -> str:
    """NFKC, ASCII quotes/dashes, casefold, collapsed whitespace, trimmed."""
    t = unicodedata.normalize("NFKC", text or "").translate(_TRANSLATE).casefold()
    return _WS_RE.sub(" ", t).strip()


def _abbreviations(text: str) -> set[str]:
    return set(ABBREVIATION_RE.findall(text or ""))


def words(text: str) -> list[str]:
    return _TOKEN_RE.findall(normalise(text))


def _contains(seq: list[str], sub: list[str]) -> bool:
    n = len(sub)
    first = sub[0]
    return any(seq[i] == first and seq[i:i + n] == sub for i in range(len(seq) - n + 1))


def _edit_distance(a: str, b: str, cap: int) -> int:
    """Levenshtein distance, banded; returns cap + 1 once it exceeds cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    big = cap + 1
    prev = {j: j for j in range(0, min(len(b), cap) + 1)}
    for i in range(1, len(a) + 1):
        cur = {}
        lo, hi = max(0, i - cap), min(len(b), i + cap)
        for j in range(lo, hi + 1):
            if j == 0:
                cur[j] = i
                continue
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev.get(j, big) + 1, cur.get(j - 1, big) + 1, prev.get(j - 1, big) + cost)
        if min(cur.values()) > cap:
            return big
        prev = cur
    return min(prev.get(len(b), big), big)


def _in_order_share(qw: list[str], window: list[str]) -> float:
    """Share of the quote's words that appear in the window in order (LCS / len)."""
    prev = [0] * (len(window) + 1)
    for x in qw:
        cur = [0]
        for j, y in enumerate(window, start=1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1] / len(qw)


def quote_match(quote: str, source: str) -> str:
    """'exact', 'inexact' or 'none' (see module docstring)."""
    qw, sw = words(quote), words(source)
    if not qw:
        return "none"
    if _contains(sw, qw):
        return "exact"
    qs, n, k = " ".join(qw), len(qw), INEXACT_MAX_CHAR_DIFF
    # Candidate alignments: any source position sharing a word with the quote,
    # shifted by that word's offset in the quote.
    positions: dict[str, list[int]] = {}
    for i, w in enumerate(qw):
        positions.setdefault(w, []).append(i)
    starts = {j - i for j, w in enumerate(sw) for i in positions.get(w, ())}
    for st in sorted(starts):
        for length in range(max(1, n - k), n + k + 1):
            lo = max(0, st)
            window = sw[lo:st + length]
            if not window:
                continue
            if _edit_distance(qs, " ".join(window), k) <= k and _in_order_share(qw, window) >= INEXACT_MIN_WORD_SHARE:
                return "inexact"
    return "none"


def quote_grounded(quote: str, source: str) -> bool:
    return quote_match(quote, source) == "exact"


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
    match = quote_match(quote, source_text)
    if match == "inexact":
        flags.append(QUOTE_INEXACT)
    elif match == "none":
        flags.append(UNGROUNDED_QUOTE)
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
