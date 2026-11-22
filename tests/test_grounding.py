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


@pytest.mark.parametrize("quote, atom, altered", [
    ("the VXUS position is down 3%", "Tom said the VXUS position is down 3%.", False),
    ("the VXUS position is down 3%", "Tom said the Vanguard Total International position is down 3%.", True),
    ("the VXUS position is down 3%", "Tom said the vxus position is down 3%.", True),  # case-sensitive
    ("the S3 bucket", "The S3 bucket is full.", False),                              # digits allowed
    ("the S3 bucket", "The storage bucket is full.", True),
    ("no codes here", "Nothing to check.", False),
])
def test_abbreviation_altered(quote, atom, altered):
    assert g.abbreviation_altered(quote, atom) is altered


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


def test_run_checks_order_and_band_rule():
    flags = g.run_checks("She held the VTIAX position.", "made up quote", SOURCE)
    assert flags == ["ungrounded_quote", "unsupported_term", "no_subject"]
    assert g.lowers_band(flags)
    assert not g.lowers_band(["abbreviation_altered", "unsupported_term", "no_subject"])


def test_verification_record():
    assert g.verification_record([], False) == {
        "checks": "v1", "flags": [], "quote_found": True, "band_downgraded": False,
    }
    assert g.verification_record(["ungrounded_quote"], True)["quote_found"] is False
