"""v2 Phase 2: decision questions and state templates (docs/phase2-design.md §2)."""

from datetime import datetime, timezone

from mnemo.server.decisions import questions as q


def test_pair_questions_are_all_four_every_time():
    qs = q.pair_questions()
    assert list(qs) == ["relation", "change", "same_attribute", "correction"]
    assert list(qs["relation"]["criteria"]) == ["same", "replaces", "refines", "conflict", "unrelated"]
    assert all(qs[k]["type"] == "noul" for k in ("change", "same_attribute", "correction"))


def test_atom_questions_time_prefilter():
    assert list(q.atom_questions(False)) == ["kind", "grounded"]
    assert list(q.atom_questions(True)) == ["kind", "grounded", "time_unresolved"]
    assert list(q.kind_question()["criteria"]) == ["state", "plan", "observation", "rule"]


def test_atom_state_format():
    d = datetime(2026, 11, 23, tzinfo=timezone.utc)
    s = q.atom_state("Mira ran 6 km.", "Mira ran 6 km today.", "mira", None, d, d, d, "day")
    assert s.splitlines() == [
        "CONTEXT: written by mira; note dated 2026-11-23; stored 2026-11-23.",
        "QUOTE:", "Mira ran 6 km today.", "ATOM:", "Mira ran 6 km.", "EVENT DATE: 2026-11-23 (precision: day)"]
    assert "note dated undated" in q.atom_state("x", None, None, None, None, None, None, "unknown")


def test_pair_state_format():
    old = {"text": "Baseline is 52.", "kind": "state", "valid_from": None, "precision": "unknown", "origin_class": "unspecified"}
    new = {"text": "Baseline is 54.", "kind": "state", "valid_from": datetime(2026, 11, 2), "precision": "day",
           "origin_class": "measurement"}
    s = q.pair_state(old, new)
    assert s.startswith("EXISTING ATOM (kind state; event date none, precision unknown; origin unspecified):\nBaseline is 52.")
    assert "\n\nNEW ATOM (kind state; event date 2026-11-02, precision day; origin measurement):\nBaseline is 54." in s


def test_q2_same_wording():
    assert q.QUESTIONS_VERSION == "q2"
    assert q.RELATION_CRITERIA["same"] == (
        "Both state the same fact about the same thing. A state or rule restated later with the same value "
        "counts as the same. Two observations are the same only if they describe the same single event.")
