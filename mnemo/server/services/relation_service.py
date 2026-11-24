"""Relations decided on write (docs/phase2-design.md §4–§5).

    prepare_relations  (no connection held during model calls)
        fetch candidates on ONE short connection → per-atom questions (kind,
        grounding, time) + kinds for unknown candidates → per-pair questions,
        all under the store's budget; unfinished pairs are `pending`
    persist_relations  (inside the store's persist transaction)
        relation_decisions rows; in enforce mode also edges, status,
        valid_until, conflicts and evidence
    process_pending    (background worker)
        re-asks pending pairs and records / applies them

Shadow mode writes relation_decisions only: no edges, no status changes, no
conflicts, and kinds learned for OLD atoms stay in `inputs`. Logs carry ids,
counts and timings only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from ..config import settings
from ..decisions import DecisionModel, DecisionResult, get_decision_model
from ..decisions.questions import (
    KINDS, QUESTIONS_VERSION, atom_questions, atom_state, kind_question, pair_questions, pair_state,
)
from . import relations
from .relations import Answers, Decision, Side, Thresholds, decide

logger = logging.getLogger(__name__)

# Wide relative-time list (Phase 1 follow-up count, 2026-11-24) and explicit dates.
RELATIVE_TIME_RE = re.compile(
    r"\b(today|tomorrow|yesterday|tonight|this morning|next week|last week|this week|last night|next month|"
    r"last month|soon|recently|currently|now|(on|this|next|last)\s+(monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday))\b", re.IGNORECASE)
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
EXPLICIT_DATE_RE = re.compile(
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b{_MON}\.?\s+\d{{1,2}}(st|nd|rd|th)?\b|\b\d{{1,2}}(st|nd|rd|th)?\s+{_MON}\b",
    re.IGNORECASE)

LINK_RELATIONS = ("same", "replaces", "refines", "conflict", "review")


def thresholds() -> Thresholds:
    return Thresholds(
        same=settings.relation_threshold_same, replaces=settings.relation_threshold_replaces,
        conflict=settings.relation_threshold_conflict, refines=settings.relation_threshold_refines,
        change=settings.relation_threshold_change, same_attribute=settings.relation_threshold_same_attribute,
        correction=settings.relation_threshold_correction, kind=settings.kind_threshold,
    )


def time_prefilter(text: str, event_date: datetime | None) -> bool:
    return event_date is None and bool(RELATIVE_TIME_RE.search(text)) and not EXPLICIT_DATE_RE.search(text)


# ── dates ───────────────────────────────────────────────────────────────────

def _explicit_day_month(text: str, d: datetime) -> bool:
    day, mon = d.day, d.strftime("%b").lower()
    if d.strftime("%Y-%m-%d") in text:
        return True
    pat = rf"\b({day}(st|nd|rd|th)?\s+{mon}[a-z]*|{mon}[a-z]*\.?\s+{day}(st|nd|rd|th)?)\b"
    return re.search(pat, text, re.IGNORECASE) is not None


def legacy_dates(text: str, remembered_on: datetime | None, created_at: datetime) -> tuple[datetime, str, str]:
    """(valid_from, precision, basis) for an atom stored before Phase 2 — the
    backfill 8.1 rule, also used on the fly for candidates (design §4, §9)."""
    if remembered_on is None:
        return created_at, "day", "stated"
    r = remembered_on.astimezone(timezone.utc) if remembered_on.tzinfo else remembered_on
    midnight = (r.hour, r.minute, r.second, r.microsecond) == (0, 0, 0, 0)
    if midnight and r.day == 1 and not _explicit_day_month(text, r):
        return remembered_on, ("year" if r.month == 1 else "month"), "event"
    return remembered_on, "day", "event"


def write_dates(kind: str, event_date: datetime | None, precision: str,
                note_date: datetime | None) -> tuple[datetime | None, str, str]:
    """(valid_from, precision, basis) for a new atom (G1 change 2). valid_from
    None means "the store time" (created_at), with basis 'stated'."""
    if kind == "plan":
        return (note_date, "day", "event") if note_date else (None, "day", "stated")
    if event_date is not None:
        return event_date, precision if precision in ("day", "month", "year") else "day", "event"
    if note_date is not None:
        return note_date, "day", "event"
    return None, "day", "stated"


# ── plan objects ────────────────────────────────────────────────────────────

@dataclass
class AtomClass:
    kind: str = "unknown"
    kind_p: float | None = None
    kind_probs: dict | None = None
    grounded_p: float | None = None
    time_unresolved: bool = False
    asked_time: bool = False
    model: str | None = None
    error: str | None = None


@dataclass
class Candidate:
    id: UUID
    text: str
    kind: str
    kind_p: float | None
    valid_from: datetime
    precision: str
    basis: str
    origin_class: str
    time_unresolved: bool
    source_id: UUID | None
    store_id: UUID | None
    alpha: float
    beta: float
    cosine: float
    quote: str | None = None
    speaker: str | None = None
    remembered_on: datetime | None = None
    created_at: datetime | None = None
    lazy: AtomClass | None = None      # kind learned this store for an `unknown` candidate

    @property
    def effective_kind(self) -> str:
        return self.lazy.kind if self.lazy is not None and self.kind == "unknown" else self.kind

    def side(self) -> Side:
        return Side(kind=self.effective_kind, valid_from=self.valid_from, precision=self.precision,
                    basis=self.basis, origin_class=self.origin_class, time_unresolved=self.time_unresolved)


@dataclass
class PairResult:
    index: int                          # new atom index in the store
    cand: Candidate
    result: DecisionResult | None       # None: never asked (budget)
    decision: Decision | None           # None: pending
    inputs: dict = field(default_factory=dict)
    probabilities: dict | None = None


@dataclass
class RelationPlan:
    mode: str
    classes: list[AtomClass]
    sides: list[Side]
    pairs: list[PairResult]
    budget_exhausted: bool = False
    timings_ms: dict = field(default_factory=dict)
    cost: float = 0.0


# ── candidates ──────────────────────────────────────────────────────────────

_CAND_SQL = """
SELECT a.id, a.text_content, a.kind, a.kind_p, a.valid_from, a.date_precision, a.date_basis,
       a.remembered_on, a.created_at, a.verification, a.source_id, a.source_quote,
       a.confidence_alpha, a.confidence_beta, s.store_id, s.origin_class, s.speaker,
       1 - (a.embedding <=> $1::vector) AS cosine
FROM atoms a
LEFT JOIN sources s ON s.id = a.source_id
WHERE a.agent_id = $2 AND a.is_active AND a.status = 'active'
  AND ($3::uuid IS NULL OR a.source_id IS DISTINCT FROM $3::uuid)
  AND NOT (a.id = ANY($5::uuid[]))
ORDER BY a.embedding <=> $1::vector
LIMIT $4
"""


def _verification(v) -> dict:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


def candidate_from_row(r) -> Candidate:
    if r["date_basis"]:
        vf, prec, basis = r["valid_from"] or r["created_at"], r["date_precision"], r["date_basis"]
    else:
        vf, prec, basis = legacy_dates(r["text_content"], r["remembered_on"], r["created_at"])
    ver = _verification(r["verification"])
    return Candidate(
        id=r["id"], text=r["text_content"], kind=r["kind"], kind_p=r["kind_p"], valid_from=vf, precision=prec,
        basis=basis, origin_class=r["origin_class"] or "unspecified",
        time_unresolved=bool(ver.get("time_unresolved")), source_id=r["source_id"], store_id=r["store_id"],
        alpha=r["confidence_alpha"], beta=r["confidence_beta"], cosine=float(r["cosine"]),
        quote=r["source_quote"], speaker=r["speaker"], remembered_on=r["remembered_on"], created_at=r["created_at"],
    )


async def fetch_candidates(conn: asyncpg.Connection, agent_id: UUID, embeddings: list, source_id: UUID | None,
                           k: int, exclude: list[UUID] | None = None) -> list[list[Candidate]]:
    out = []
    for emb in embeddings:
        rows = await conn.fetch(_CAND_SQL, emb, agent_id, source_id, k, list(exclude or []))
        out.append([candidate_from_row(r) for r in rows])
    return out


# ── asking ──────────────────────────────────────────────────────────────────

def _class_from(res: DecisionResult, asked_time: bool, th: Thresholds) -> AtomClass:
    c = AtomClass(asked_time=asked_time, model=res.model, error=res.error)
    probs = res.probabilities("kind")
    if probs:
        top = max(probs, key=probs.get)
        c.kind_probs, c.kind_p = probs, float(probs[top])
        c.kind = top if top in KINDS and c.kind_p >= th.kind else "unknown"
    c.grounded_p = res.p_true("grounded")
    if asked_time:
        p = res.p_true("time_unresolved")
        c.time_unresolved = p is not None and p >= 0.5
    return c


def _answers(res: DecisionResult) -> Answers | None:
    probs = res.probabilities("relation")
    if not probs:
        return None
    return Answers(relation={k: float(v) for k, v in probs.items()},
                   change=float(res.p_true("change") or 0.0),
                   same_attribute=float(res.p_true("same_attribute") or 0.0),
                   correction=float(res.p_true("correction") or 0.0))


def _side_dict(text: str, side: Side) -> dict:
    return {"text": text, "kind": side.kind, "valid_from": side.valid_from, "precision": side.precision,
            "origin_class": side.origin_class}


async def prepare_relations(pool: asyncpg.Pool, agent_id: UUID, source_id: UUID | None, req, atoms: list,
                            mode: str | None = None, model: DecisionModel | None = None,
                            origin_class: str = "unspecified") -> RelationPlan | None:
    """`atoms`: PreparedAtom list (candidate + embedding). Returns None when relations are off."""
    mode = mode or settings.relation_mode
    if mode == "off" or not atoms:
        return None
    model = model or get_decision_model()
    th = thresholds()
    t0 = time.monotonic()
    deadline = t0 + settings.relation_budget_seconds
    plan = RelationPlan(mode=mode, classes=[AtomClass() for _ in atoms], sides=[], pairs=[])

    async with pool.acquire() as conn:
        cands = await fetch_candidates(conn, agent_id, [p.embedding for p in atoms], source_id, settings.relation_k)
    t1 = time.monotonic()
    plan.timings_ms["candidates"] = round((t1 - t0) * 1000)

    if model is None:                                   # no client: everything pending (fail open)
        plan.sides = [_new_side(p.candidate, AtomClass(), req, origin_class) for p in atoms]
        plan.pairs = [PairResult(i, c, None, None) for i, cs in enumerate(cands) for c in cs]
        plan.budget_exhausted = True
        return plan

    sem = asyncio.Semaphore(settings.relation_concurrency)

    async def ask(state, questions) -> DecisionResult:
        async with sem:
            if time.monotonic() >= deadline:
                return DecisionResult(answers=None, error="deadline")
            try:
                return await model.ask(state, questions, deadline=deadline)
            except Exception as exc:          # fail open; ZdrViolation included (nothing was sent)
                logger.exception("decision model raised")
                return DecisionResult(answers=None, error=f"client error: {type(exc).__name__}")

    # Phase A: per-atom questions, plus kinds for unknown candidates (each once).
    note_date = req.remembered_on

    async def classify_new(i, p):
        cand = p.candidate
        tp = time_prefilter(cand.text, cand.event_date)
        state = atom_state(cand.text, cand.source_quote, req.speaker, req.addressee, note_date,
                           datetime.now(timezone.utc), cand.event_date, cand.date_precision)
        res = await ask(state, atom_questions(tp))
        plan.cost += res.cost or 0.0
        plan.classes[i] = _class_from(res, tp, th) if res.ok else AtomClass(asked_time=tp, error=res.error)

    unknown = {c.id: c for cs in cands for c in cs if c.kind == "unknown"}

    async def classify_old(c: Candidate):
        state = atom_state(c.text, c.quote, c.speaker, None, c.remembered_on, c.created_at,
                           c.valid_from if c.basis == "event" else None, c.precision)
        res = await ask(state, {"kind": kind_question()})
        plan.cost += res.cost or 0.0
        lazy = _class_from(res, False, th) if res.ok else AtomClass(error=res.error)
        for cs in cands:
            for x in cs:
                if x.id == c.id:
                    x.lazy = lazy

    await asyncio.gather(*(classify_new(i, p) for i, p in enumerate(atoms)),
                         *(classify_old(c) for c in unknown.values()))
    t2 = time.monotonic()
    plan.timings_ms["classify"] = round((t2 - t1) * 1000)
    plan.sides = [_new_side(p.candidate, plan.classes[i], req, origin_class) for i, p in enumerate(atoms)]

    # Phase B: every pair, all four questions (G1 change 6).
    async def one_pair(i, c: Candidate) -> PairResult:
        new_side, old_side = plan.sides[i], c.side()
        state = pair_state(_side_dict(c.text, old_side), _side_dict(atoms[i].candidate.text, new_side))
        res = await ask(state, pair_questions())
        plan.cost += res.cost or 0.0
        inputs = {"new": new_side.to_json(), "old": old_side.to_json(), "cosine": round(c.cosine, 4),
                  "questions": QUESTIONS_VERSION, "new_kind_p": plan.classes[i].kind_p,
                  "old_kind_p": c.lazy.kind_p if c.lazy else c.kind_p, "old_kind_lazy": c.lazy is not None,
                  "old_kind_stored": c.kind}
        ans = _answers(res) if res.ok else None
        if ans is None:
            return PairResult(i, c, res, None, inputs)
        probs = {"relation": ans.relation, "change": ans.change, "same_attribute": ans.same_attribute,
                 "correction": ans.correction}
        return PairResult(i, c, res, decide(new_side, old_side, ans, th), inputs, probs)

    plan.pairs = list(await asyncio.gather(*(one_pair(i, c) for i, cs in enumerate(cands) for c in cs)))
    t3 = time.monotonic()
    plan.timings_ms["pairs"] = round((t3 - t2) * 1000)
    plan.budget_exhausted = any(p.decision is None for p in plan.pairs)
    return plan


def _new_side(cand, cls: AtomClass, req, origin_class: str) -> Side:
    vf, prec, basis = write_dates(cls.kind, cand.event_date, cand.date_precision, req.remembered_on)
    return Side(kind=cls.kind, valid_from=vf or datetime.now(timezone.utc), precision=prec, basis=basis,
                origin_class=origin_class, time_unresolved=cls.time_unresolved)


def atom_fields(plan: RelationPlan | None, i: int, cand, req) -> dict:
    """kind / kind_p / valid_from / date_precision / date_basis for the INSERT,
    and extra verification keys. Dates are set even when relations are off."""
    cls = plan.classes[i] if plan else AtomClass()
    vf, prec, basis = write_dates(cls.kind, cand.event_date, cand.date_precision, req.remembered_on)
    extra = {}
    if plan:
        extra["questions"] = QUESTIONS_VERSION
        if cls.grounded_p is not None:
            extra["grounded_p"] = round(cls.grounded_p, 4)
        if cls.asked_time:
            extra["time_unresolved"] = cls.time_unresolved
        if cls.error:
            extra["decision_error"] = cls.error
    if cand.event_date is not None and cls.kind == "plan":
        extra["planned_date"] = cand.event_date.strftime("%Y-%m-%d")
    return {"kind": cls.kind, "kind_p": cls.kind_p, "valid_from": vf, "date_precision": prec,
            "date_basis": basis, "verification_extra": extra}


# ── persist ─────────────────────────────────────────────────────────────────

async def _record(conn, agent_id, new_id, source_id, pr: PairResult, mode, final, guard, raw, edge_id=None,
                  decided_by="relation_v2", attempts=None) -> UUID:
    res = pr.result
    return await conn.fetchval(
        """
        INSERT INTO relation_decisions (agent_id, new_atom_id, old_atom_id, source_id, model, probabilities, inputs,
            raw_choice, final_relation, guard_fired, mode, decided_by, edge_id, attempts, latency_ms, cost, decided_at)
        VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                CASE WHEN $9 = 'pending' THEN NULL ELSE now() END)
        RETURNING id
        """,
        agent_id, new_id, pr.cand.id, source_id, res.model if res else None,
        json.dumps(pr.probabilities) if pr.probabilities else None, json.dumps(pr.inputs, default=str),
        raw, final, guard, mode, decided_by, edge_id,
        attempts if attempts is not None else (res.attempts if res else 0) or 1,
        res.latency_ms if res else None, res.cost if res else None,
    )


async def _edge(conn, src: UUID, tgt: UUID, edge_type: str, weight: float, decided_by: str, meta: dict) -> UUID | None:
    return await conn.fetchval(
        """
        INSERT INTO edges (source_id, target_id, edge_type, weight, decided_by, metadata)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
        RETURNING id
        """,
        src, tgt, edge_type, max(0.0, min(1.0, weight)), decided_by, json.dumps(meta, default=str),
    )


async def apply(conn: asyncpg.Connection, agent_id: UUID, new_id: UUID, new_source_id: UUID | None,
                new_store_id: UUID | None, new_alpha_beta: tuple[float, float], pr: PairResult,
                final: str, decided_by: str = "relation_v2") -> tuple[str, str | None, UUID | None]:
    """Enforce one decision. Returns (final, extra guard, edge id). Re-checks
    the old atom is still active (else `review`, guard `stale`)."""
    from .atom_service import bayesian_merge_damped
    if final == "unrelated":
        return final, None, None
    old = await conn.fetchrow(
        "SELECT status, valid_from, confidence_alpha, confidence_beta FROM atoms WHERE id = $1 FOR UPDATE",
        pr.cand.id)
    guard = None
    if old is None or old["status"] != "active":
        if final in ("same", "replaces"):
            final, guard = "review", "stale"
    weight = (pr.probabilities or {}).get("relation", {}).get(pr.decision.raw if pr.decision else final, 1.0)
    meta = {"decision_raw": pr.decision.raw if pr.decision else None, "questions": QUESTIONS_VERSION}
    if final == "same":
        independent = (pr.cand.source_id is None or pr.cand.source_id != new_source_id) and \
                      (pr.cand.store_id is None or pr.cand.store_id != new_store_id)
        meta.update({"evidence_added": independent, "old_alpha": old["confidence_alpha"],
                     "old_beta": old["confidence_beta"]})
        edge_id = await _edge(conn, new_id, pr.cand.id, "same", weight, decided_by, meta)
        await conn.execute("UPDATE atoms SET status = 'merged' WHERE id = $1", new_id)
        if independent:
            a, b = bayesian_merge_damped(old["confidence_alpha"], old["confidence_beta"], *new_alpha_beta)
            await conn.execute("UPDATE atoms SET confidence_alpha = $1, confidence_beta = $2 WHERE id = $3",
                               a, b, pr.cand.id)
        return final, guard, edge_id
    if final == "replaces":
        new_vf = await conn.fetchval("SELECT COALESCE(valid_from, created_at) FROM atoms WHERE id = $1", new_id)
        meta["old_valid_until"] = None
        edge_id = await _edge(conn, new_id, pr.cand.id, "replaces", weight, decided_by, meta)
        await conn.execute("UPDATE atoms SET status = 'superseded', valid_until = $2 WHERE id = $1",
                           pr.cand.id, new_vf)
        return final, guard, edge_id
    edge_id = await _edge(conn, new_id, pr.cand.id, final, weight, decided_by, meta)
    if final == "conflict":
        await conn.execute(
            """
            INSERT INTO conflicts (agent_id, atom_a, atom_b)
            VALUES ($1, $2, $3)
            ON CONFLICT (LEAST(atom_a, atom_b), GREATEST(atom_a, atom_b)) WHERE resolved_at IS NULL DO NOTHING
            """,
            agent_id, pr.cand.id, new_id)
    return final, guard, edge_id


async def persist_relations(conn: asyncpg.Connection, agent_id: UUID, plan: RelationPlan,
                            new_ids: list[UUID | None], dedup_targets: list[UUID | None],
                            source_id: UUID | None, store_id: UUID | None,
                            alpha_beta: list[tuple[float, float]]) -> dict:
    """Write one relation_decisions row per pair; in enforce mode apply them.
    Returns {"relations": [[...] per atom], "pending": [atom ids], counts}."""
    per_atom: list[list[dict]] = [[] for _ in new_ids]
    pending: list[UUID] = []
    counts: dict[str, int] = {}
    # At most one `same` per new atom: the most probable; others become refines.
    best_same: dict[int, float] = {}
    for pr in plan.pairs:
        if pr.decision and pr.decision.final == "same":
            p = pr.probabilities["relation"].get("same", 0.0)
            best_same[pr.index] = max(best_same.get(pr.index, 0.0), p)
    merged_atoms: set[int] = set()
    order = sorted(plan.pairs, key=lambda pr: (pr.index, 0 if pr.decision and pr.decision.final == "same" else 1))
    for pr in order:
        new_id = new_ids[pr.index]
        if new_id is None and dedup_targets[pr.index] is not None:
            pr.inputs["dedup_target"] = str(dedup_targets[pr.index])
        if pr.decision is None:
            guard = None if new_id else "dedup_merged_no_retry"
            attempts = None if new_id else settings.relation_pending_max_attempts
            rid = await _record(conn, agent_id, new_id, source_id, pr, plan.mode, "pending", guard, None,
                                attempts=attempts)
            if new_id:
                pending.append(new_id)
            counts["pending"] = counts.get("pending", 0) + 1
            continue
        final, guard = pr.decision.final, pr.decision.guard
        if final == "same" and pr.probabilities["relation"].get("same", 0.0) < best_same.get(pr.index, 0.0):
            final, guard = "refines", "second_same"
        if final == "replaces" and pr.index in merged_atoms:
            final, guard = "review", "new_merged"
        edge_id = None
        if plan.mode == "enforce" and pr.cand.lazy is not None and pr.cand.lazy.kind != "unknown":
            # Enforce: a kind learned for an `unknown` old atom is kept (design §4).
            await conn.execute("UPDATE atoms SET kind = $2, kind_p = $3 WHERE id = $1 AND kind = 'unknown'",
                               pr.cand.id, pr.cand.lazy.kind, pr.cand.lazy.kind_p)
        if plan.mode == "enforce" and new_id is not None:
            final, extra, edge_id = await apply(conn, agent_id, new_id, source_id, store_id,
                                                alpha_beta[pr.index], pr, final)
            guard = extra or guard
            if final == "same":
                merged_atoms.add(pr.index)
        rid = await _record(conn, agent_id, new_id, source_id, pr, plan.mode, final, guard, pr.decision.raw,
                            edge_id=edge_id)
        counts[final] = counts.get(final, 0) + 1
        if final in LINK_RELATIONS:
            per_atom[pr.index].append({"relation": final, "target_atom_id": str(pr.cand.id),
                                       "decision_id": str(rid), "edge_id": str(edge_id) if edge_id else None,
                                       "mode": plan.mode})
    return {"relations": per_atom, "pending": sorted(set(pending), key=str), "counts": counts}


# ── pending worker ──────────────────────────────────────────────────────────

_ONE_SQL = """
SELECT a.id, a.text_content, a.kind, a.kind_p, a.valid_from, a.date_precision, a.date_basis,
       a.remembered_on, a.created_at, a.verification, a.source_id, a.source_quote,
       a.confidence_alpha, a.confidence_beta, s.store_id, s.origin_class, s.speaker, 0.0 AS cosine
FROM atoms a
LEFT JOIN sources s ON s.id = a.source_id
WHERE a.id = $1 AND a.agent_id = $2 AND a.is_active AND a.status = 'active'
"""

_PENDING_SQL = """
SELECT d.id, d.agent_id, d.new_atom_id, d.old_atom_id, d.source_id, d.mode, d.attempts, d.inputs,
       n.text_content AS new_text, n.kind AS new_kind, n.valid_from AS new_valid_from,
       n.date_precision AS new_precision, n.date_basis AS new_basis, n.verification AS new_verification,
       n.confidence_alpha AS new_alpha, n.confidence_beta AS new_beta, n.created_at AS new_created_at,
       ns.origin_class AS new_origin, ns.store_id AS new_store_id
FROM relation_decisions d
JOIN atoms n ON n.id = d.new_atom_id
LEFT JOIN sources ns ON ns.id = n.source_id
WHERE d.final_relation = 'pending' AND d.attempts < $1
  AND d.created_at < now() - make_interval(secs => LEAST(1800, 30 * power(2, d.attempts)))
ORDER BY d.created_at
LIMIT $2
"""


async def process_pending(pool: asyncpg.Pool, model: DecisionModel | None = None, limit: int = 50) -> dict:
    model = model or get_decision_model()
    counts = {"picked": 0, "decided": 0, "still_pending": 0}
    if model is None:
        return counts
    th = thresholds()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_PENDING_SQL, settings.relation_pending_max_attempts, limit)
        olds = {}
        for r in rows:
            o = await conn.fetchrow(_ONE_SQL, r["old_atom_id"], r["agent_id"])
            olds[r["id"]] = o
    counts["picked"] = len(rows)
    for r in rows:
        o = olds.get(r["id"])
        if o is None:
            async with pool.acquire() as conn:
                await conn.execute("UPDATE relation_decisions SET final_relation = 'review', guard_fired = 'stale', "
                                   "decided_at = now() WHERE id = $1 AND final_relation = 'pending'", r["id"])
            counts["decided"] += 1
            continue
        cand = candidate_from_row(o)
        ver = _verification(r["new_verification"])
        new_side = Side(kind=r["new_kind"], valid_from=r["new_valid_from"] or r["new_created_at"],
                        precision=r["new_precision"], basis=r["new_basis"] or "stated",
                        origin_class=r["new_origin"] or "unspecified", time_unresolved=bool(ver.get("time_unresolved")))
        if cand.kind == "unknown":
            st = atom_state(cand.text, cand.quote, cand.speaker, None, cand.remembered_on, cand.created_at,
                            cand.valid_from if cand.basis == "event" else None, cand.precision)
            res_k = await model.ask(st, {"kind": kind_question()})
            if res_k.ok:
                cand.lazy = _class_from(res_k, False, th)
        old_side = cand.side()
        state = pair_state(_side_dict(cand.text, old_side), _side_dict(r["new_text"], new_side))
        res = await model.ask(state, pair_questions())
        ans = _answers(res) if res.ok else None
        async with pool.acquire() as conn:
            async with conn.transaction():
                if ans is None:
                    await conn.execute("UPDATE relation_decisions SET attempts = attempts + 1 WHERE id = $1", r["id"])
                    counts["still_pending"] += 1
                    continue
                dec = decide(new_side, old_side, ans, th)
                probs = {"relation": ans.relation, "change": ans.change, "same_attribute": ans.same_attribute,
                         "correction": ans.correction}
                inputs = _verification(r["inputs"])
                inputs.update({"new": new_side.to_json(), "old": old_side.to_json(), "questions": QUESTIONS_VERSION,
                               "old_kind_lazy": cand.lazy is not None, "retried": True})
                pr = PairResult(0, cand, res, dec, inputs, probs)
                final, guard, edge_id = dec.final, dec.guard, None
                if r["mode"] == "enforce":
                    final, extra, edge_id = await apply(conn, r["agent_id"], r["new_atom_id"], r["source_id"],
                                                        r["new_store_id"], (r["new_alpha"], r["new_beta"]), pr, final)
                    guard = extra or guard
                await conn.execute(
                    """
                    UPDATE relation_decisions SET model = $2, probabilities = $3::jsonb, inputs = $4::jsonb,
                        raw_choice = $5, final_relation = $6, guard_fired = $7, edge_id = $8,
                        attempts = attempts + 1, latency_ms = $9, cost = $10, decided_at = now()
                    WHERE id = $1 AND final_relation = 'pending'
                    """,
                    r["id"], res.model, json.dumps(probs), json.dumps(inputs, default=str), dec.raw, final, guard,
                    edge_id, res.latency_ms, res.cost)
                counts["decided"] += 1
    if counts["picked"]:
        logger.info("relation_pending", extra={"event": "relation_pending", **counts})
    return counts


async def pending_loop(pool: asyncpg.Pool) -> None:
    while True:
        await asyncio.sleep(settings.relation_pending_interval_seconds)
        if settings.relation_mode == "off":
            continue
        try:
            await process_pending(pool)
        except Exception:
            logger.exception("relation pending worker failed")
