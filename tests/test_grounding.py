"""v2 Phase 1: deterministic checks (synthetic strings only)."""

import pytest

from mnemo.server.services import grounding as g

SOURCE = "Tom said the VXUS position is down 3% since the “Q3” review.\nThe AAPL note is in the ops wiki."


@pytest.mark.parametrize("quote, grounded", [
    ("the VXUS position is down 3%", True),
    ("THE vxus   position\nis DOWN 3%", True),          # case + whitespace
    ('since the "Q3" review', True),                   # curly vs straight quotes
    ("the VXUS position fell 3%", False),              # paraphrase
    ("the VXUS position ... review", False),           # ellipsis join
    ("", False),
])
def test_ungrounded_quote(quote, grounded):
    assert g.quote_grounded(quote, SOURCE) is grounded


def test_multi_sentence_contiguous_quote_is_grounded():
    assert g.quote_grounded("since the “Q3” review. The AAPL note", SOURCE)


@pytest.mark.parametrize("atom, unsupported", [
    ("Tom said the VXUS position is down 3%.", False),
    ("Tom said the VTIAX position is down 3%.", True),   # model introduced a code
    ("Tom reviewed the position.", False),
])
def test_unsupported_term(atom, unsupported):
    assert g.unsupported_term(atom, SOURCE) is unsupported


@pytest.mark.parametrize("atom, flagged", [
    ("She moved the review to Thursday.", True),
    ("This is the plan for November.", True),
    ("went camping in July 2026", True),
    ("Tom prefers tea in the morning.", False),
    ("The billing service retries failed charges three times.", False),
    ("2026-11-20: Tom moved the review.", False),
    ("VXUS fell 3% in November.", False),
])
def test_no_subject(atom, flagged):
    assert g.no_subject(atom) is flagged


@pytest.mark.parametrize("atom, names, flagged", [
    ("clio prefers aisle seats.", ("clio",), False),
    ("lat-adcf00 plans a trip to Lisbon.", ("lat-adcf00",), False),
    ("clio prefers aisle seats.", (), True),
    ("clioxyz prefers tea.", ("clio",), True),     # prefix of a longer word is not the name
])
def test_no_subject_known_names(atom, names, flagged):
    assert g.no_subject(atom, names) is flagged


def test_run_checks_order_and_band_rule():
    flags = g.run_checks("She held the VTIAX position.", "made up quote", SOURCE)
    assert flags == ["ungrounded_quote", "unsupported_term", "no_subject"]
    assert g.lowers_band(flags)
    assert not g.lowers_band(["unsupported_term", "no_subject"])


def test_verification_record():
    assert g.verification_record([], False) == {
        "checks": "v2", "flags": [], "quote_found": True, "band_downgraded": False,
    }
    assert g.verification_record(["ungrounded_quote"], True)["quote_found"] is False


# ── v2: word-sequence grounding (the five patterns seen in production, synthetic text) ──

NOTE = (
    "Weekly ops notes\n"
    "**Backups**\n"
    "- the nightly backup rotation\nkeeps thirty days of snapshots on the archive volume\n"
    "- Mira checks the restore drill every Friday before the team standup\n"
    "Next: the billing service retries failed charges three times, then pages the on-call engineer.\n"
)


@pytest.mark.parametrize("quote, expected", [
    # 1. line break in the source where the quote has punctuation → exact
    ("the nightly backup rotation: keeps thirty days of snapshots", "exact"),
    # 2. bullet + line break in the source, sentence punctuation in the quote → exact
    ("on the archive volume. Mira checks the restore drill every Friday", "exact"),
    # 3. one letter changed next to a bullet (inflection) → inexact
    ("keeps thirty days of snapshots on the archive volume. Mira check the restore drill every Friday", "inexact"),
    # 4. one letter plus one punctuation mark changed → inexact
    ("the billing service retry failed charges three times; then pages the on-call engineer", "inexact"),
    # 5. two letters changed in two places (21 words, 19 unchanged) → inexact
    ("every Friday before the team standup. Next: the billing services retries failed charge three times, "
     "then pages the on-call engineer", "inexact"),
    # ...but the same two changes in a 15-word quote are under 90% of the words → ungrounded
    ("Next: the billing services retries failed charge three times, then pages the on-call engineer", "none"),
    # markdown in the source is ignored
    ("Backups the nightly backup rotation", "exact"),
    # more than 3 characters different → ungrounded
    ("the billing service retried failed payments three times", "none"),
    # paraphrase → ungrounded
    ("the billing system tries charges again three times", "none"),
    # short quote: one changed word is under 90% of the words → ungrounded
    ("Mira check the drill", "none"),
    ("", "none"),
])
def test_quote_match_word_sequence(quote, expected):
    assert g.quote_match(quote, NOTE) == expected


def test_quote_inexact_is_record_only():
    flags = g.run_checks("Mira checks the restore drill.",
                         "Mira check the restore drill every Friday before the team standup", NOTE)
    assert flags == ["quote_inexact"]
    assert not g.lowers_band(flags)
    rec = g.verification_record(flags, False)
    assert rec["quote_found"] is True and rec["band_downgraded"] is False


def test_ungrounded_quote_still_lowers_band():
    flags = g.run_checks("Mira runs drills.", "Mira runs the drill on Mondays", NOTE)
    assert flags == ["ungrounded_quote"]
    assert g.lowers_band(flags)


def test_quote_match_long_source_is_fast():
    import time
    src = (NOTE * 60)[:8000]
    t = time.monotonic()
    assert g.quote_match("the billing service retry failed charges three times; then pages the on-call engineer", src) == "inexact"
    assert g.quote_match("an entirely invented sentence that shares the and of words", src) == "none"
    assert time.monotonic() - t < 1.0
