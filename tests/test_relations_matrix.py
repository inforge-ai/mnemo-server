"""v2 Phase 2: kind matrix and global guards (pure; docs/phase2-design.md §3)."""

from datetime import datetime, timezone

import pytest

from mnemo.server.services import relations as r
from mnemo.server.services.relations import Answers, Side, Thresholds, decide, replay

# Independent transcription of the spec's matrix (phase2-spec.md "The kind matrix").
SPEC = {
    "state":       {"state": "replaces†, refines, conflict", "plan": "replaces‡, refines",
                    "observation": "refines", "rule": "refines, conflict"},
    "plan":        {"state": "refines", "plan": "replaces†, refines", "observation": "refines", "rule": "refines"},
    "observation": {"state": "replaces‡, refines, conflict", "plan": "replaces‡, refines",
                    "observation": "replaces§, refines, conflict", "rule": "refines, conflict"},
    "rule":        {"state": "refines", "plan": "refines", "observation": "refines",
                    "rule": "replaces†, refines, conflict"},
}
MARK = {"†": r.DAGGER, "‡": r.DDAGGER, "§": r.SECTION}


def spec_cell(new, old):
    out = {}
    for item in SPEC[new][old].split(", "):
        name = item.rstrip("†‡§")
        out[name] = MARK.get(item[-1]) if item[-1] in MARK else None
    if new == old:
        out["same"] = None
    return out


D1 = datetime(2026, 11, 20, tzinfo=timezone.utc)
D2 = datetime(2026, 11, 23, tzinfo=timezone.utc)


def side(kind, when=D2, precision="day", basis="event", origin="unspecified", unresolved=False):
    return Side(kind=kind, valid_from=when, precision=precision, basis=basis,
                origin_class=origin, time_unresolved=unresolved)


def ans(raw, p=0.95, refines=0.03, change=0.9, attr=0.9, corr=0.9):
    probs = {k: 0.0 for k in r.RELATIONS}
    probs[raw] = p
    if raw != "refines":
        probs["refines"] = refines
    rest = [k for k in r.RELATIONS if k not in (raw, "refines")]
    left = max(0.0, 1 - sum(probs.values()))
    for k in rest:
        probs[k] = left / len(rest)
    return Answers(relation=probs, change=change, same_attribute=attr, correction=corr)


CELLS = [(n, o) for n in r.KINDS for o in r.KINDS]


@pytest.mark.parametrize("new, old", CELLS)
def test_matrix_matches_spec(new, old):
    assert r.cell(new, old) == spec_cell(new, old)


@pytest.mark.parametrize("new, old", CELLS)
@pytest.mark.parametrize("raw", r.RELATIONS)
def test_every_cell_every_raw_choice(new, old, raw):
    """All conditions met, both dated on the same day (so § can pass): the
    result is the raw choice when the cell allows it, otherwise a downgrade."""
    d = decide(side(new, D2), side(old, D2), ans(raw))
    if raw == "unrelated":
        assert (d.final, d.guard) == ("unrelated", None)
    elif raw in spec_cell(new, old):
        assert (d.final, d.guard) == (raw, None)
    else:
        assert (d.final, d.guard) == ("unrelated", "matrix")      # refines p below threshold


def test_downgrade_to_refines_when_refines_probable():
    d = decide(side("plan"), side("state"), ans("replaces", p=0.6, refines=0.35), Thresholds(refines=0.3))
    assert (d.final, d.guard) == ("refines", "matrix")


@pytest.mark.parametrize("kinds", [("unknown", "state"), ("state", "unknown"), ("unknown", "unknown")])
@pytest.mark.parametrize("raw, final", [("replaces", "unrelated"), ("same", "unrelated"),
                                        ("conflict", "unrelated"), ("refines", "refines")])
def test_unknown_kind_only_refines(kinds, raw, final):
    assert decide(side(kinds[0]), side(kinds[1]), ans(raw)).final == final


# ── † same subject + attribute + compatible origin ──
def test_dagger_needs_same_attribute():
    d = decide(side("state"), side("state", D1), ans("replaces", attr=0.2))
    assert (d.final, d.guard) == ("unrelated", "dagger")


def test_dagger_fails_on_two_specified_different_origins():
    d = decide(side("state", origin="measurement"), side("state", D1, origin="user_stated"), ans("replaces"))
    assert (d.final, d.guard) == ("unrelated", "dagger")


@pytest.mark.parametrize("o_new, o_old", [("unspecified", "measurement"), ("user_stated", "unspecified"),
                                          ("measurement", "measurement")])
def test_dagger_unspecified_is_a_wildcard(o_new, o_old):
    d = decide(side("state", origin=o_new), side("state", D1, origin=o_old), ans("replaces"))
    assert (d.final, d.guard) == ("replaces", None)


# ── ‡ the change question ──
def test_ddagger_reading_against_baseline_is_not_a_change():
    d = decide(side("observation"), side("state", D1), ans("replaces", change=0.1))
    assert (d.final, d.guard) == ("unrelated", "ddagger")


def test_ddagger_planned_then_done():
    assert decide(side("observation"), side("plan", D1), ans("replaces", change=0.95)).final == "replaces"


# ── § observation correction ──
@pytest.mark.parametrize("new, old, why", [
    (side("observation", D2), side("observation", D1), "different days"),
    (side("observation", D2, basis="stated"), side("observation", D2), "stated basis"),
    (side("observation", D2, precision="month"), side("observation", D2), "month precision"),
])
def test_section_needs_same_event_day(new, old, why):
    d = decide(new, old, ans("replaces"))
    assert (d.final, d.guard) == ("unrelated", "section"), why


def test_section_needs_correction():
    d = decide(side("observation"), side("observation"), ans("replaces", corr=0.2))
    assert (d.final, d.guard) == ("unrelated", "section")


def test_section_correction_replaces():
    assert decide(side("observation"), side("observation"), ans("replaces")).final == "replaces"


# ── per-relation thresholds ──
@pytest.mark.parametrize("raw, p", [("replaces", 0.79), ("same", 0.84), ("conflict", 0.59)])
def test_threshold(raw, p):
    d = decide(side("state"), side("state", D1), ans(raw, p=p))
    assert (d.final, d.guard) == ("unrelated", "threshold")


def test_thresholds_are_configurable():
    th = Thresholds(replaces=0.5)
    assert decide(side("state"), side("state", D1), ans("replaces", p=0.6), th).final == "replaces"


# ── global guards on replaces ──
def test_origin_guard_conflict_where_cell_allows_it():
    d = decide(side("observation", origin="measurement"), side("state", D1, origin="user_stated"), ans("replaces"))
    assert (d.final, d.guard) == ("conflict", "origin_differs")


def test_origin_guard_review_where_cell_has_no_conflict():
    d = decide(side("state", origin="measurement"), side("plan", D1, origin="user_stated"), ans("replaces"))
    assert (d.final, d.guard) == ("review", "origin_differs")


@pytest.mark.parametrize("new, old", [
    (side("state", precision="year"), side("state", D1)),
    (side("state"), side("state", D1, precision="unknown")),
    (side("state", when=None), side("state", D1)),
])
def test_precision_guard(new, old):
    d = decide(new, old, ans("replaces"))
    assert (d.final, d.guard) == ("review", "precision")


def test_time_unresolved_guard():
    d = decide(side("plan", unresolved=True), side("plan", D1), ans("replaces"))
    assert (d.final, d.guard) == ("review", "time_unresolved")


def test_date_order_guard_older_over_newer():
    d = decide(side("state", D1), side("state", D2), ans("replaces"))
    assert (d.final, d.guard) == ("review", "date_order")


def test_date_order_at_coarser_precision():
    same_month = side("state", datetime(2026, 11, 1, tzinfo=timezone.utc), precision="month")
    assert decide(same_month, side("state", D2), ans("replaces")).final == "replaces"     # not earlier at month
    earlier = side("state", datetime(2026, 10, 1, tzinfo=timezone.utc), precision="month")
    assert decide(earlier, side("state", D2), ans("replaces")).guard == "date_order"


def test_ordering_guards_accept_stated_basis():
    d = decide(side("state", basis="stated"), side("state", D1, basis="stated"), ans("replaces"))
    assert (d.final, d.guard) == ("replaces", None)


# ── fixtures-in-miniature from the spec ──
def test_series_on_two_days_hides_nothing():
    d = decide(side("observation", D2), side("observation", D1), ans("replaces"))
    assert d.final != "replaces"


def test_observation_against_rule_never_retires_it():
    assert decide(side("observation"), side("rule", D1), ans("replaces")).final != "replaces"


def test_replay_matches_decide():
    new, old, a = side("state", origin="measurement"), side("state", D1), ans("replaces", p=0.82)
    inputs = {"new": new.to_json(), "old": old.to_json()}
    probs = {"relation": a.relation, "change": a.change, "same_attribute": a.same_attribute, "correction": a.correction}
    assert replay(inputs, probs) == decide(new, old, a)
    assert replay(inputs, probs, Thresholds(replaces=0.9)).guard == "threshold"


def test_replay_reapplies_the_kind_threshold():
    a = ans("replaces", p=0.95)
    new = side("unknown")              # stored as unknown at kind threshold 0.8
    inputs = {"new": new.to_json(), "old": side("state", D1).to_json(),
              "new_kind_top": "state", "new_kind_p": 0.7, "old_kind_stored": "state"}
    probs = {"relation": a.relation, "change": a.change, "same_attribute": a.same_attribute, "correction": a.correction}
    assert replay(inputs, probs, Thresholds(kind=0.8)).final == "unrelated"
    assert replay(inputs, probs, Thresholds(kind=0.6)).final == "replaces"


@pytest.mark.parametrize("new, old, ok", [
    (side("observation", D2), side("observation", D2), True),                       # same event day
    (side("observation", D2), side("observation", D1), False),                      # different days
    (side("observation", D2, basis="stated"), side("observation", D2), False),
    (side("observation", D2, precision="month"), side("observation", D2), False),
])
def test_same_between_observations_needs_one_event_day(new, old, ok):
    d = decide(new, old, ans("same"))
    assert (d.final, d.guard) == (("same", None) if ok else ("unrelated", "same_observation"))


def test_same_for_states_restated_later_is_allowed():
    assert decide(side("state", D2), side("state", D1), ans("same")).final == "same"
