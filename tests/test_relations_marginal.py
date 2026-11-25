"""The marginal kind rule (Tom, 2026-11-25).

P(allowed) = sum over (k_new, k_old) of p_new(k_new) * p_old(k_old) *
1[raw allowed in the cell and its conditions pass]. Act if P(allowed) >= the
kind threshold, otherwise downgrade as decide() does. No unknown kind.
Pure functions; no I/O.
"""

import itertools
from datetime import datetime, timezone

import pytest

from mnemo.server.services.relations import (
    KINDS, RELATIONS, Answers, Side, Thresholds, decide, decide_marginal, kind_distribution, p_allowed, replay,
)

SYM = "state_rule_symmetric"


def onehot(k):
    return {x: (1.0 if x == k else 0.0) for x in KINDS}


def side(kind="unknown", day=20, probs=None, origin="document", precision="day", basis="event"):
    return Side(kind=kind, valid_from=datetime(2026, 11, day, tzinfo=timezone.utc), precision=precision,
                basis=basis, origin_class=origin, kind_probs=probs)


def answers(raw, p=0.95, change=0.9, attr=0.9, corr=0.9, refines=0.0):
    rel = {r: 0.0 for r in RELATIONS}
    rel[raw] = p
    rel["refines"] = max(rel["refines"], refines)
    return Answers(relation=rel, change=change, same_attribute=attr, correction=corr)


# ── one-hot distributions reproduce the argmax rule ─────────────────────────

CONDS = list(itertools.product([0.9, 0.1], repeat=3))


@pytest.mark.parametrize("matrix", ["as_built", SYM])
@pytest.mark.parametrize("kn,ko", list(itertools.product(KINDS, KINDS)))
def test_onehot_equals_argmax(matrix, kn, ko):
    for raw in RELATIONS:
        for chg, attr, corr in CONDS:
            for day in (20, 24):              # same day / different day (§ and the same-observation guard)
                new, old = side(kn, 24, onehot(kn)), side(ko, day, onehot(ko))
                ans = answers(raw, change=chg, attr=attr, corr=corr)
                a = decide(new, old, ans, Thresholds(matrix=matrix))
                m = decide(new, old, ans, Thresholds(matrix=matrix, kind_rule="marginal"))
                assert m.final == a.final, (raw, kn, ko, chg, attr, corr, day, a, m)


def test_onehot_from_known_kind_without_probs():
    assert kind_distribution(side("rule")) == onehot("rule")
    assert kind_distribution(side("unknown")) is None


def test_distribution_is_renormalised_over_the_four_kinds():
    d = kind_distribution(side(probs={"state": 0.4, "rule": 0.4, "plan": 0.1, "observation": 0.0, "other": 0.1}))
    assert sum(d.values()) == pytest.approx(1.0) and d["state"] == pytest.approx(0.4 / 0.9)


# ── P(allowed) ──────────────────────────────────────────────────────────────

def test_p_allowed_hand_computed():
    """New state 0.9 / rule 0.1; old state 0.5 / rule 0.5; replaces; change and same_attribute pass.
    As built: (state,state) dagger passes; (rule,rule) dagger passes; (state,rule), (rule,state) no replaces.
    P = 0.9*0.5 + 0.1*0.5 = 0.50. Symmetric adds (state,rule) and (rule,state): 0.9*0.5 + 0.1*0.5 → 1.0."""
    new = side(probs={"state": 0.9, "rule": 0.1, "plan": 0.0, "observation": 0.0})
    old = side(probs={"state": 0.5, "rule": 0.5, "plan": 0.0, "observation": 0.0})
    ans = answers("replaces")
    assert p_allowed("replaces", new, old, ans, Thresholds()) == pytest.approx(0.50)
    assert p_allowed("replaces", new, old, ans, Thresholds(matrix=SYM)) == pytest.approx(1.0)


def test_conditions_are_inside_the_sum():
    """Without same_attribute the dagger and ddagger_attr cells drop out; ddagger (state<-plan) stays."""
    new = side(probs=onehot("state"))
    old = side(probs={"state": 0.3, "plan": 0.4, "observation": 0.3, "rule": 0.0})
    ans = answers("replaces", attr=0.1)
    assert p_allowed("replaces", new, old, ans, Thresholds()) == pytest.approx(0.4)


@pytest.mark.parametrize("th, final, guard", [(0.5, "replaces", None), (0.6, "unrelated", "marginal")])
def test_acts_at_or_above_the_kind_threshold(th, final, guard):
    new = side(probs={"state": 0.9, "rule": 0.1, "plan": 0.0, "observation": 0.0}, day=24)
    old = side(probs={"state": 0.5, "rule": 0.5, "plan": 0.0, "observation": 0.0}, day=20)
    d = decide_marginal(new, old, answers("replaces"), Thresholds(kind=th))
    assert (d.final, d.guard) == (final, guard)
    assert d.notes == ["p_allowed=0.5000"]


def test_below_threshold_downgrades_to_refines_when_refines_is_likely():
    new = side(probs=onehot("state"), day=24)
    old = side(probs={"state": 0.3, "rule": 0.7, "plan": 0.0, "observation": 0.0})
    d = decide_marginal(new, old, answers("replaces", p=0.6, refines=0.55), Thresholds())
    assert (d.final, d.guard) == ("refines", "marginal")


def test_relation_threshold_still_applies():
    new, old = side(probs=onehot("state"), day=24), side(probs=onehot("state"))
    d = decide_marginal(new, old, answers("replaces", p=0.7), Thresholds())
    assert (d.final, d.guard) == ("unrelated", "threshold")


def test_global_guards_still_apply():
    new, old = side(probs=onehot("state"), day=18), side(probs=onehot("state"), day=20)
    assert decide_marginal(new, old, answers("replaces"), Thresholds()).guard == "date_order"
    # (observation, state): replaces needs change and same_attribute, not origin; the cell allows conflict
    new = side(probs=onehot("observation"), day=24, origin="measurement")
    d = decide_marginal(new, side(probs=onehot("state")), answers("replaces"), Thresholds())
    assert (d.final, d.guard) == ("conflict", "origin_differs")
    a = decide(side("observation", 24, origin="measurement"), side("state"), answers("replaces"), Thresholds())
    assert (a.final, a.guard) == (d.final, d.guard)


def test_no_distribution_and_unknown_kind_is_the_unknown_cell():
    new, old = side(probs=onehot("state"), day=24), side("unknown")
    assert decide_marginal(new, old, answers("replaces"), Thresholds()).guard == "marginal"
    assert decide_marginal(new, old, answers("refines", p=0.9), Thresholds()).final == "refines"


def test_no_unknown_kind_in_the_variant():
    """A side whose top kind is below the kind threshold (unknown under argmax) still counts."""
    new = side(probs=onehot("state"), day=24)
    old = side("unknown", probs={"state": 0.55, "plan": 0.15, "observation": 0.15, "rule": 0.15})
    ans = answers("replaces", change=0.9, attr=0.9)
    assert decide(new, old, ans, Thresholds()).final == "unrelated"          # argmax: unknown cell
    assert decide(new, old, ans, Thresholds(kind_rule="marginal")).final == "replaces"


# ── the schedule-change pattern (fixture schedule_change_rule_state) ────────

def test_schedule_change_under_the_three_variants():
    """Live numbers: new state 0.96; old rule 0.54 (else state); replaces p 1.0, change 0.98, attr 0.96."""
    new = side("state", 24, {"state": 0.96, "plan": 0.02, "observation": 0.01, "rule": 0.01})
    old = side("unknown", 20, {"rule": 0.54, "state": 0.40, "plan": 0.03, "observation": 0.03})
    ans = answers("replaces", p=1.0, change=0.98, attr=0.96, corr=0.0)
    results = {}
    for name, th in [("as_built", Thresholds()), ("symmetric", Thresholds(matrix=SYM)),
                     ("symmetric+marginal", Thresholds(matrix=SYM, kind_rule="marginal"))]:
        results[name] = decide(new, old, ans, th).final
    assert results == {"as_built": "unrelated", "symmetric": "unrelated", "symmetric+marginal": "replaces"}


# ── replay ──────────────────────────────────────────────────────────────────

def _row(with_probs=True):
    new = side("state", 24, {"state": 0.96, "plan": 0.02, "observation": 0.01, "rule": 0.01} if with_probs else None)
    old = side("unknown", 20, {"rule": 0.54, "state": 0.40, "plan": 0.03, "observation": 0.03} if with_probs else None)
    inputs = {"new": new.to_json(), "old": old.to_json(), "new_kind_top": "state", "new_kind_p": 0.96,
              "old_kind_top": "rule", "old_kind_p": 0.54, "old_kind_lazy": True, "old_kind_stored": "unknown"}
    probs = {"relation": {"replaces": 1.0, "unrelated": 0.0}, "change": 0.98, "same_attribute": 0.96,
             "correction": 0.0}
    return inputs, probs


def test_replay_grid_from_stored_distributions():
    inputs, probs = _row()
    grid = {}
    for variant, base in [("as_built", {}), ("symmetric", {"matrix": SYM}),
                          ("symmetric+marginal", {"matrix": SYM, "kind_rule": "marginal"})]:
        for k in (0.5, 0.6, 0.7):
            grid[(variant, k)] = replay(inputs, probs, Thresholds(kind=k, **base)).final
    assert grid == {
        ("as_built", 0.5): "unrelated", ("as_built", 0.6): "unrelated", ("as_built", 0.7): "unrelated",
        # old rule 0.54 passes 0.5 → (state, rule) replaces under the symmetric matrix
        ("symmetric", 0.5): "replaces", ("symmetric", 0.6): "unrelated", ("symmetric", 0.7): "unrelated",
        # P(allowed) = 0.96*(0.54+0.40) + small ≈ 0.91
        ("symmetric+marginal", 0.5): "replaces", ("symmetric+marginal", 0.6): "replaces",
        ("symmetric+marginal", 0.7): "replaces",
    }


def test_replay_takes_missing_distributions_from_atoms():
    """Rows written before migration 012 carry no distributions; the replay supplies atoms.kind_probs."""
    inputs, probs = _row(with_probs=False)
    th = Thresholds(matrix=SYM, kind_rule="marginal")
    assert replay(inputs, probs, th).final == "unrelated"        # old: unknown, no distribution
    kp = {"new": {"state": 0.96, "plan": 0.02, "observation": 0.01, "rule": 0.01},
          "old": {"rule": 0.54, "state": 0.40, "plan": 0.03, "observation": 0.03}}
    assert replay(inputs, probs, th, kind_probs=kp).final == "replaces"


def test_stored_distribution_wins_over_supplied():
    inputs, probs = _row()
    kp = {"new": onehot("plan"), "old": onehot("plan")}
    assert replay(inputs, probs, Thresholds(matrix=SYM, kind_rule="marginal"), kind_probs=kp).final == "replaces"


def test_side_json_round_trip_keeps_distribution():
    s = side("state", 24, {"state": 0.7, "plan": 0.1, "observation": 0.1, "rule": 0.1})
    assert Side.from_json(s.to_json()).kind_probs == s.kind_probs
