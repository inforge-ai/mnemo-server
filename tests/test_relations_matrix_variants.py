"""The G3 matrix variant (Tom, 2026-11-25): state <-> rule symmetric.

As built, (state, rule) allows refines and conflict and (rule, state) only
refines. The variant allows replaces both ways when change AND same_attribute
pass (as state <-> observation), and refines and conflict both ways. Every
other cell is the same. Pure functions; no I/O.
"""

from datetime import datetime, timezone

import pytest

from mnemo.server.services.relations import (
    DDAGGER_ATTR, KINDS, MATRICES, MATRIX, Answers, Side, Thresholds, cell, decide, replay,
)

SYM = "state_rule_symmetric"


def test_variant_differs_only_in_state_rule_cells():
    diff = {k for k in MATRIX if MATRICES[SYM][k] != MATRIX[k]}
    assert diff == {("state", "rule"), ("rule", "state")}
    for k in diff:
        assert MATRICES[SYM][k] == {"replaces": DDAGGER_ATTR, "refines": None, "conflict": None}


def test_default_is_as_built():
    assert Thresholds().matrix == "as_built"
    for n in KINDS:
        for o in KINDS:
            assert cell(n, o) == cell(n, o, "as_built")


def _side(kind, day):
    return Side(kind=kind, valid_from=datetime(2026, 11, day, tzinfo=timezone.utc), precision="day",
                basis="event", origin_class="document")


def _ans(change=0.9, same_attribute=0.9):
    return Answers(relation={"replaces": 0.9, "refines": 0.06, "unrelated": 0.04},
                   change=change, same_attribute=same_attribute)


@pytest.mark.parametrize("new_kind,old_kind", [("state", "rule"), ("rule", "state")])
def test_schedule_change_replaces_only_under_variant(new_kind, old_kind):
    """The 03:00 -> 04:00 smoke-test pattern: a schedule (classed rule or state) is changed."""
    new, old = _side(new_kind, 24), _side(old_kind, 20)
    assert decide(new, old, _ans()).final == "unrelated"
    assert decide(new, old, _ans()).guard == "matrix"
    assert decide(new, old, _ans(), Thresholds(matrix=SYM)).final == "replaces"


@pytest.mark.parametrize("change,same_attribute", [(0.2, 0.9), (0.9, 0.2)])
def test_variant_replaces_needs_change_and_same_attribute(change, same_attribute):
    d = decide(_side("state", 24), _side("rule", 20), _ans(change, same_attribute), Thresholds(matrix=SYM))
    assert (d.final, d.guard) == ("unrelated", "ddagger_attr")   # refines p 0.06 < 0.50


def test_variant_rule_state_allows_conflict():
    ans = Answers(relation={"conflict": 0.8, "unrelated": 0.2})
    assert decide(_side("rule", 24), _side("state", 20), ans).final == "unrelated"
    assert decide(_side("rule", 24), _side("state", 20), ans, Thresholds(matrix=SYM)).final == "conflict"


def test_replay_under_both_matrices():
    inputs = {"new": _side("state", 24).to_json(), "old": {**_side("unknown", 20).to_json()},
              "new_kind_top": "state", "new_kind_p": 0.96,
              "old_kind_top": "rule", "old_kind_p": 0.68, "old_kind_lazy": True, "old_kind_stored": "unknown"}
    probs = {"relation": {"replaces": 0.9, "unrelated": 0.1}, "change": 0.9, "same_attribute": 0.9,
             "correction": 0.0}
    assert replay(inputs, probs).final == "unrelated"
    assert replay(inputs, probs, Thresholds(matrix=SYM)).final == "replaces"
