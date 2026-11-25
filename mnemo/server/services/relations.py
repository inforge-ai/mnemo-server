"""Relation decision: the kind matrix and global guards (docs/phase2-design.md §3).

Pure functions, no I/O. `decide()` depends only on what is stored in a
relation_decisions row (inputs + probabilities), so any decision can be
replayed at other thresholds (`replay`), as the G3 grid does.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

KINDS = ("state", "plan", "observation", "rule")
RELATIONS = ("same", "replaces", "refines", "conflict", "unrelated")
FINAL = RELATIONS + ("review", "pending")

DAGGER, DDAGGER, SECTION = "dagger", "ddagger", "section"
# state <-> observation (Tom, 2026-11-24): the change question (‡) AND the
# same_attribute part of † must both pass; origin is not part of this condition.
DDAGGER_ATTR = "ddagger_attr"

# (new kind, old kind) -> {relation: extra condition or None}. `same` is added
# by `cell()` where the kinds are equal and known. Spec matrix, phase2-spec.md.
MATRIX: dict[tuple[str, str], dict[str, str | None]] = {
    ("state", "state"):             {"replaces": DAGGER, "refines": None, "conflict": None},
    ("state", "plan"):              {"replaces": DDAGGER, "refines": None},
    ("state", "observation"):       {"replaces": DDAGGER_ATTR, "refines": None, "conflict": None},
    ("state", "rule"):              {"refines": None, "conflict": None},
    ("plan", "state"):              {"refines": None},
    ("plan", "plan"):               {"replaces": DAGGER, "refines": None},
    ("plan", "observation"):        {"refines": None},
    ("plan", "rule"):               {"refines": None},
    ("observation", "state"):       {"replaces": DDAGGER_ATTR, "refines": None, "conflict": None},
    ("observation", "plan"):        {"replaces": DDAGGER, "refines": None},
    ("observation", "observation"): {"replaces": SECTION, "refines": None, "conflict": None},
    ("observation", "rule"):        {"refines": None, "conflict": None},
    ("rule", "state"):              {"refines": None},
    ("rule", "plan"):               {"refines": None},
    ("rule", "observation"):        {"refines": None},
    ("rule", "rule"):               {"replaces": DAGGER, "refines": None, "conflict": None},
}
UNKNOWN_CELL: dict[str, str | None] = {"refines": None}

# G3 candidate (Tom, 2026-11-25): state <-> rule symmetric, like state <-> observation:
# replaces both ways when change AND same_attribute pass; refines and conflict both ways.
# Used for replay and fixture runs only, until Tom picks a matrix at G3.
MATRIX_STATE_RULE_SYMMETRIC: dict[tuple[str, str], dict[str, str | None]] = {
    **MATRIX,
    ("state", "rule"): {"replaces": DDAGGER_ATTR, "refines": None, "conflict": None},
    ("rule", "state"): {"replaces": DDAGGER_ATTR, "refines": None, "conflict": None},
}
MATRICES: dict[str, dict[tuple[str, str], dict[str, str | None]]] = {
    "as_built": MATRIX,
    "state_rule_symmetric": MATRIX_STATE_RULE_SYMMETRIC,
}


@dataclass
class Side:
    kind: str = "unknown"
    valid_from: datetime | None = None
    precision: str = "unknown"            # day | month | year | unknown
    basis: str | None = None              # event | stated
    origin_class: str = "unspecified"
    time_unresolved: bool = False
    kind_probs: dict[str, float] | None = None   # the full kind distribution (marginal variant)

    def to_json(self) -> dict:
        d = asdict(self)
        d["valid_from"] = self.valid_from.isoformat() if self.valid_from else None
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Side":
        d = dict(d)
        if d.get("valid_from"):
            d["valid_from"] = datetime.fromisoformat(d["valid_from"])
        return cls(**{k: d.get(k, getattr(cls(), k)) for k in cls.__dataclass_fields__})


@dataclass
class Answers:
    relation: dict[str, float]            # probabilities over RELATIONS
    change: float = 0.0                   # ‡
    same_attribute: float = 0.0           # †
    correction: float = 0.0               # §

    @property
    def raw(self) -> str:
        return max(RELATIONS, key=lambda r: self.relation.get(r, 0.0))

    def p(self, rel: str) -> float:
        return float(self.relation.get(rel, 0.0))


@dataclass
class Thresholds:
    same: float = 0.85
    replaces: float = 0.80
    conflict: float = 0.60
    refines: float = 0.50
    change: float = 0.50
    same_attribute: float = 0.50
    correction: float = 0.50
    kind: float = 0.60   # Tom, 2026-11-24
    matrix: str = "as_built"   # a key of MATRICES
    # "argmax": each side's kind is its top kind if p >= kind, else unknown (as built).
    # "marginal": decide_marginal() sums over both kind distributions (G3 candidate, Tom 2026-11-25).
    kind_rule: str = "argmax"


    def for_relation(self, rel: str) -> float:
        return getattr(self, rel)


@dataclass
class Decision:
    final: str
    guard: str | None
    raw: str
    notes: list[str] = field(default_factory=list)


def cell(new_kind: str, old_kind: str, matrix: str = "as_built") -> dict[str, str | None]:
    if new_kind not in KINDS or old_kind not in KINDS:
        return dict(UNKNOWN_CELL)
    allowed = dict(MATRICES[matrix][(new_kind, old_kind)])
    if new_kind == old_kind:
        allowed["same"] = None
    return allowed


def origin_compatible(a: Side, b: Side) -> bool:
    """'unspecified' is a wildcard (G1 change 1)."""
    return "unspecified" in (a.origin_class, b.origin_class) or a.origin_class == b.origin_class


_ORDER = {"day": 0, "month": 1, "year": 2}


def _coarser(p1: str, p2: str) -> str:
    return p1 if _ORDER[p1] >= _ORDER[p2] else p2


def _trunc(d: datetime, precision: str) -> tuple:
    if precision == "day":
        return (d.year, d.month, d.day)
    if precision == "month":
        return (d.year, d.month)
    return (d.year,)


def earlier_at_coarser(new: Side, old: Side) -> bool:
    """New valid_from earlier than old's, compared at the coarser precision."""
    p = _coarser(new.precision, old.precision)
    return _trunc(new.valid_from, p) < _trunc(old.valid_from, p)


def _dated(s: Side) -> bool:
    return s.valid_from is not None and s.precision in ("day", "month")


def _same_day(a: Side, b: Side) -> bool:
    return a.valid_from is not None and b.valid_from is not None and a.valid_from.date() == b.valid_from.date()


def _downgrade(ans: Answers, th: Thresholds, raw: str, guard: str) -> Decision:
    return Decision("refines" if ans.p("refines") >= th.refines else "unrelated", guard, raw)


def _cell_guard(raw: str, cond: str | None, new_kind: str, old_kind: str, new: Side, old: Side,
                ans: Answers, th: Thresholds) -> str | None:
    """The guard that fails for `raw` in the cell (new_kind, old_kind), or None.
    `raw` is already known to be allowed in the cell."""
    if cond == DAGGER and not (ans.same_attribute >= th.same_attribute and origin_compatible(new, old)):
        return "dagger"
    if cond == DDAGGER and ans.change < th.change:
        return "ddagger"
    if cond == DDAGGER_ATTR and not (ans.change >= th.change and ans.same_attribute >= th.same_attribute):
        return "ddagger_attr"
    if cond == SECTION and not (new.precision == "day" and old.precision == "day"
                                and new.basis == "event" and old.basis == "event"
                                and _same_day(new, old) and ans.correction >= th.correction):
        return "section"
    if raw == "same" and new_kind == "observation" and old_kind == "observation" and not (
            new.precision == "day" and old.precision == "day"
            and new.basis == "event" and old.basis == "event" and _same_day(new, old)):
        return "same_observation"   # two observations: same only for one event
    return None


def _global_guards(raw: str, new: Side, old: Side, conflict_allowed: bool) -> Decision | None:
    if raw != "replaces":
        return None
    if not origin_compatible(new, old):              # both specified and different
        return Decision("conflict" if conflict_allowed else "review", "origin_differs", raw)
    if not (_dated(new) and _dated(old)):
        return Decision("review", "precision", raw)
    if new.time_unresolved or old.time_unresolved:
        return Decision("review", "time_unresolved", raw)
    if earlier_at_coarser(new, old):
        return Decision("review", "date_order", raw)
    return None


def decide(new: Side, old: Side, ans: Answers, th: Thresholds | None = None) -> Decision:
    th = th or Thresholds()
    if th.kind_rule == "marginal":
        return decide_marginal(new, old, ans, th)
    raw = ans.raw
    if raw == "unrelated":
        return Decision("unrelated", None, raw)
    allowed = cell(new.kind, old.kind, th.matrix)
    if raw not in allowed:
        return _downgrade(ans, th, raw, "matrix")
    guard = _cell_guard(raw, allowed[raw], new.kind, old.kind, new, old, ans, th)
    if guard:
        return _downgrade(ans, th, raw, guard)
    if ans.p(raw) < th.for_relation(raw):
        return _downgrade(ans, th, raw, "threshold")
    return _global_guards(raw, new, old, "conflict" in allowed) or Decision(raw, None, raw)


# ── marginal variant (G3 candidate; Tom, 2026-11-25) ────────────────────────

def kind_distribution(side: Side) -> dict[str, float] | None:
    """The side's distribution over the four kinds, renormalised over KINDS.
    No distribution stored: one-hot on a known kind, None for unknown."""
    if side.kind_probs:
        d = {k: max(0.0, float(side.kind_probs.get(k, 0.0))) for k in KINDS}
        total = sum(d.values())
        if total > 0:
            return {k: v / total for k, v in d.items()}
    if side.kind in KINDS:
        return {k: (1.0 if k == side.kind else 0.0) for k in KINDS}
    return None


def p_allowed(rel: str, new: Side, old: Side, ans: Answers, th: Thresholds) -> float:
    """P(allowed) = sum over (k_new, k_old) of p_new(k_new) * p_old(k_old) *
    1[rel allowed in the cell and its conditions pass]. A side with no
    distribution and an unknown kind uses the unknown cell (refines only)."""
    dn, do = kind_distribution(new), kind_distribution(old)
    if dn is None or do is None:
        return 1.0 if rel in UNKNOWN_CELL else 0.0
    total = 0.0
    for kn, pn in dn.items():
        for ko, po in do.items():
            w = pn * po
            if w <= 0.0:
                continue
            allowed = cell(kn, ko, th.matrix)
            if rel in allowed and _cell_guard(rel, allowed[rel], kn, ko, new, old, ans, th) is None:
                total += w
    return total


def decide_marginal(new: Side, old: Side, ans: Answers, th: Thresholds | None = None) -> Decision:
    """No kind threshold on either side and no unknown kind: act on `raw` if
    P(allowed) >= th.kind, otherwise downgrade as decide() does."""
    th = th or Thresholds()
    raw = ans.raw
    if raw == "unrelated":
        return Decision("unrelated", None, raw)
    pa = p_allowed(raw, new, old, ans, th)
    note = [f"p_allowed={pa:.4f}"]
    if pa < th.kind:
        d = _downgrade(ans, th, raw, "marginal")
        d.notes = note
        return d
    if ans.p(raw) < th.for_relation(raw):
        d = _downgrade(ans, th, raw, "threshold")
        d.notes = note
        return d
    conflict_ok = p_allowed("conflict", new, old, ans, th) >= th.kind
    d = _global_guards(raw, new, old, conflict_ok) or Decision(raw, None, raw)
    d.notes = note
    return d


def _replay_kind(side: Side, top: str | None, p: float | None, th: Thresholds) -> Side:
    """Re-apply the kind threshold when the row recorded the model's top kind."""
    if top in KINDS and p is not None:
        side.kind = top if p >= th.kind else "unknown"
    return side


def replay(inputs: dict, probabilities: dict, th: Thresholds | None = None,
           kind_probs: dict | None = None) -> Decision:
    """Re-run decide() from a stored relation_decisions row, at any thresholds
    (the kind threshold too, where the row recorded each side's top kind), any
    matrix and either kind rule. `kind_probs` = {"new": {...}, "old": {...}}
    supplies distributions a row did not record (rows written before
    migration 012: take them from atoms.kind_probs after backfill 8.3b)."""
    th = th or Thresholds()
    ans = Answers(relation=probabilities["relation"], change=probabilities.get("change", 0.0),
                  same_attribute=probabilities.get("same_attribute", 0.0),
                  correction=probabilities.get("correction", 0.0))
    new = _replay_kind(Side.from_json(inputs["new"]), inputs.get("new_kind_top"), inputs.get("new_kind_p"), th)
    old = Side.from_json(inputs["old"])
    if inputs.get("old_kind_lazy") or inputs.get("old_kind_stored") == "unknown":
        old = _replay_kind(old, inputs.get("old_kind_top"), inputs.get("old_kind_p"), th)
    for side, key in ((new, "new"), (old, "old")):
        if not side.kind_probs and kind_probs and kind_probs.get(key):
            side.kind_probs = dict(kind_probs[key])
    return decide(new, old, ans, th)
