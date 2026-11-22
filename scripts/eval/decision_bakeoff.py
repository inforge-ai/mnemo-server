"""mnemo v2 Phase 1 item 9: decision-model bake-off (no production integration).

Compares OpenRouter decision models (alpha Decisions API, read 2026-11-22 at
https://openrouter.ai/docs/api-reference/decisions) and one generative
baseline on two tasks:

  A  grounding (yes/no, `noul`): is this atom fully supported by this quote?
  B  relation (choice): same | update | contradicts | narrows | unrelated

Data rules (spec rule 4): only synthetic text and the LoCoMo corpus in
mnemo_eval go to any provider. No production memory text. The script refuses
any database other than mnemo_eval. LoCoMo-derived items and raw results stay
outside the repo (default ~/.cache/mnemo-bakeoff); synthetic data lives in
scripts/eval/data/.

Keys: OPENROUTER_API_KEY from --env-file (default ~/.config/mnemo/openrouter.env),
ANTHROPIC_API_KEY from the environment. Neither is ever printed.

Subcommands:
  build-grounding   decompose synthetic + LoCoMo passages, make perturbed negatives
  build-relation    generate ~200 balanced synthetic relation pairs (+ 40-item label check CSV)
  run               query every model on every item and variant
  report            print metrics as markdown

Example:
  UV_FROZEN=1 uv run python scripts/eval/decision_bakeoff.py build-grounding
  UV_FROZEN=1 uv run python scripts/eval/decision_bakeoff.py build-relation
  UV_FROZEN=1 uv run python scripts/eval/decision_bakeoff.py run
  UV_FROZEN=1 uv run python scripts/eval/decision_bakeoff.py report > /tmp/bakeoff.md
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "scripts" / "eval" / "data"
CACHE = Path(os.environ.get("MNEMO_BAKEOFF_DIR", Path.home() / ".cache" / "mnemo-bakeoff"))
EVAL_DB = "postgresql://mnemo_migrator@localhost:5432/mnemo_eval"

# Settings() needs a database URL; this script never uses the app's pool.
os.environ.setdefault("MNEMO_DATABASE_URL", EVAL_DB)
os.environ.setdefault("MNEMO_ENV_FILE", "/dev/null")
sys.path.insert(0, str(REPO))

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DECISION_MODELS = [
    "typesafe/jev-1.14",
    "google/gemini-decision-1",
    "openai/gpt-6-luna-decisions",
    "liquid/d1.2-ultra",
    "microsoft/microsoft-decision-1.1",
]
GENERATIVE_BASELINE = "claude-haiku-4-5-20251001"   # the production decomposer model
GENERATOR_MODEL = "claude-haiku-4-5-20251001"       # builds negatives / relation pairs
# $ per 1M tokens (input, output), from the Anthropic model table, 2026-11-22.
ANTHROPIC_PRICE = {"claude-haiku-4-5-20251001": (1.00, 5.00), "claude-haiku-5-5": (0.10, 0.50)}

RELATIONS = ["same", "update", "contradicts", "narrows", "unrelated"]
PERTURBATIONS = ["number", "abbreviation", "subject", "negation"]
VARIANTS = ["base", "order", "wording"]
CONCURRENCY = 8


# ── Helpers ─────────────────────────────────────────────────────────────────

def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def anthropic_client():
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(max_retries=3, timeout=60.0)


async def tool_call(client, model: str, system: str, user: str, name: str, schema: dict,
                    max_tokens: int = 2048) -> tuple[dict, dict]:
    """Forced tool call; returns (input, usage)."""
    resp = await client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
        tools=[{"name": name, "description": name, "input_schema": schema}],
        tool_choice={"type": "tool", "name": name},
    )
    block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
    if block is None or resp.stop_reason == "max_tokens":
        raise RuntimeError("no complete tool call")
    return block.input, {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}


# ── Task A data: grounding ──────────────────────────────────────────────────

async def locomo_passages(n: int, rng: random.Random) -> list[dict]:
    """Short passages of consecutive LoCoMo atoms from mnemo_eval (public
    benchmark text). Read-only. Refuses any other database."""
    import asyncpg
    url = os.environ.get("MNEMO_BAKEOFF_EVAL_URL", EVAL_DB)
    if not re.search(r"/mnemo_eval(\?|$)", url):
        raise SystemExit("refusing: the bake-off reads only the mnemo_eval database")
    conn = await asyncpg.connect(url, server_settings={"default_transaction_read_only": "on"})
    try:
        counts = {r["agent_id"]: r["n"] for r in await conn.fetch(
            "SELECT agent_id, count(*) AS n FROM atoms WHERE source_type = 'direct_experience' "
            "GROUP BY agent_id HAVING count(*) > 10")}
        agents = sorted(counts)
        out = []
        for i in range(n):
            agent = rng.choice(agents)
            offset = rng.randrange(0, counts[agent] - 4)
            rows = await conn.fetch(
                """
                SELECT text_content FROM atoms
                WHERE agent_id = $1 AND source_type = 'direct_experience'
                ORDER BY created_at, id OFFSET $2 LIMIT 4
                """,
                agent, offset,
            )
            text = " ".join(r["text_content"].strip() for r in rows)
            if len(text) > 80:
                out.append({"id": f"locomo{i+1:02d}", "text": text, "speaker": "the narrator",
                            "addressee": None, "date": None})
        return out
    finally:
        await conn.close()


def _context(note: dict) -> str:
    """What the decomposer knew besides the quote: who wrote it, to whom, when.
    Atoms legitimately use it (names for "I"/"you", dates), so the judge sees it too."""
    parts = [f"Written by {note.get('speaker') or 'the narrator'}"]
    if note.get("addressee"):
        parts.append(f"to {note['addressee']}")
    if note.get("date"):
        d = datetime.fromisoformat(note["date"])
        parts.append(f"on {d.strftime('%A')}, {d.day} {d.strftime('%B %Y')}")
    return " ".join(parts) + "."


def perturb_number(text: str, rng: random.Random) -> str | None:
    nums = list(re.finditer(r"\d+(?:[.,]\d+)?", text))
    if not nums:
        return None
    m = rng.choice(nums)
    raw = m.group(0)
    digits = raw.replace(",", "")
    try:
        val = float(digits)
    except ValueError:
        return None
    new = val + rng.choice([1, 2, 3, 5, 10]) if val < 100 else val * rng.choice([0.5, 1.5, 2])
    new_s = (f"{int(new):,}" if "," in raw else str(int(new))) if "." not in raw else f"{new:.1f}"
    if new_s == raw:
        return None
    return text[:m.start()] + new_s + text[m.end():]


PERTURB_SYSTEM = (
    "You edit one short factual statement to make a minimally different statement that is NOT "
    "supported by its source quote. Make exactly the requested kind of change and nothing else; "
    "keep the rest of the wording identical. Kinds: abbreviation = replace an abbreviation, ticker "
    "or code with a plausible but different expansion or name (e.g. NQ -> Nasdaq); subject = "
    "attribute the claim to a different person, team or system than the source does; negation = "
    "negate or reverse the claim. If the requested kind is impossible for this statement, set "
    "possible to false."
)
PERTURB_SCHEMA = {"type": "object", "required": ["possible", "statement"], "properties": {
    "possible": {"type": "boolean"}, "statement": {"type": "string"}}}


async def build_grounding(args) -> None:
    from mnemo.server.decomposers import DecomposeRequest, decompose_note
    from mnemo.server.decomposers.anthropic import AnthropicDecomposer
    from mnemo.server.services.grounding import ABBREVIATION_RE, quote_grounded

    rng = random.Random(args.seed)
    notes = json.loads((DATA / "synthetic_notes.json").read_text())
    for n in notes:
        n["origin"] = "synthetic"
    loco = await locomo_passages(args.locomo, rng)
    for p in loco:
        p["origin"] = "locomo"
    decomposer = AnthropicDecomposer(GENERATOR_MODEL)
    sem = asyncio.Semaphore(4)

    async def decompose(n):
        async with sem:
            req = DecomposeRequest(
                text=n["text"], agent_name="clio", speaker=n.get("speaker") or "clio",
                addressee=n.get("addressee"),
                remembered_on=datetime.fromisoformat(n["date"]) if n.get("date") else None,
            )
            res = await decompose_note(req, decomposer)
            return [(n, a) for a in res.atoms]

    pairs = [p for chunk in await asyncio.gather(*(decompose(n) for n in notes + loco)) for p in chunk]
    positives = []
    for n, a in pairs:
        if a.source_quote and quote_grounded(a.source_quote, n["text"]):
            positives.append({"note": n["id"], "origin": n["origin"], "atom": a.text, "quote": a.source_quote,
                              "context": _context(n)})
    rng.shuffle(positives)
    positives = positives[: args.max_positives]

    client = anthropic_client()

    async def negative(i, pos):
        # Rotate the primary kind for balance; fall back to the others in random order.
        rng_i = random.Random(args.seed * 1000 + i)
        primary = PERTURBATIONS[i % len(PERTURBATIONS)]
        rest = [k for k in PERTURBATIONS if k != primary]
        rng_i.shuffle(rest)
        kinds = [primary] + rest
        for kind in kinds:
            if kind == "number":
                new = perturb_number(pos["atom"], rng_i)
            else:
                if kind == "abbreviation" and not ABBREVIATION_RE.search(pos["atom"]):
                    continue
                async with sem:
                    try:
                        out, _ = await tool_call(
                            client, GENERATOR_MODEL, PERTURB_SYSTEM,
                            f"Kind: {kind}\nSource quote: {pos['quote']}\nStatement: {pos['atom']}",
                            "edited_statement", PERTURB_SCHEMA, max_tokens=512)
                    except Exception:
                        continue
                new = out.get("statement") if out.get("possible") else None
            if new and new.strip() and new.strip() != pos["atom"].strip():
                return {**pos, "atom": new.strip(), "perturbation": kind}
        return None

    negs = [n for n in await asyncio.gather(*(negative(i, p) for i, p in enumerate(positives))) if n]
    items = []
    for i, p in enumerate(positives):
        items.append({"id": f"A{i:04d}", "label": True, "perturbation": None, **p})
    for i, n in enumerate(negs):
        items.append({"id": f"A{len(positives)+i:04d}", "label": False, **n})
    write_jsonl(CACHE / "grounding_items.jsonl", items)
    c = Counter((it["origin"], it["label"], it["perturbation"]) for it in items)
    print(f"grounding items: {len(items)} ({len(positives)} supported, {len(negs)} perturbed)")
    for k, v in sorted(c.items(), key=str):
        print("  ", k, v)


# ── Task B data: relation pairs ─────────────────────────────────────────────

RELATION_SPEC = {
    "same": "Both atoms state the same fact in different words; the new atom adds nothing and changes nothing.",
    "update": "The new atom replaces the existing one: a newer value, a changed state, a completed plan, or an explicit correction. The existing atom was true or believed earlier and is no longer current.",
    "contradicts": "The atoms make incompatible claims about the same thing at the same time, and neither says it replaces or corrects the other.",
    "narrows": "The new atom qualifies, scopes or refines the existing one without making it false (adds a condition, exception or detail).",
    "unrelated": "The atoms mention overlapping people, systems or topics but state independent facts; neither affects the other.",
}
DOMAINS = [
    "software deployment and CI", "personal finance and investing", "travel plans", "health and habits",
    "team schedules and meetings", "cloud infrastructure costs", "customer support tickets",
    "home maintenance", "research projects", "vendor contracts",
]
PAIR_SYSTEM = (
    "You write synthetic test data for a memory system. Each item is a pair of short memory atoms "
    "(one sentence each, standalone, naming their subject, invented names only, no real people or "
    "companies) whose relationship is exactly the requested class. Vary sentence structure and do not "
    "make the class obvious from keywords alone (avoid always writing 'now', 'actually', 'not')."
)
PAIR_SCHEMA = {"type": "object", "required": ["pairs"], "properties": {"pairs": {"type": "array", "items": {
    "type": "object", "required": ["existing", "new"],
    "properties": {"existing": {"type": "string"}, "new": {"type": "string"}}}}}}


async def build_relation(args) -> None:
    client = anthropic_client()
    sem = asyncio.Semaphore(4)
    rng = random.Random(args.seed)

    async def batch(cls, domains, set_no):
        async with sem:
            out, _ = await tool_call(
                client, GENERATOR_MODEL, PAIR_SYSTEM,
                f"Class: {cls}\nDefinition: {RELATION_SPEC[cls]}\n"
                f"Write 10 pairs, two from each of these domains: {', '.join(domains)}. "
                f"This is set {set_no}: use different invented names and situations from other sets.",
                "pairs", PAIR_SCHEMA, max_tokens=4096)
            return [(cls, p) for p in out.get("pairs", [])]

    jobs = []
    for cls in RELATIONS:
        for set_no in range(1, 6):   # 5 sets x 10 pairs, then capped at per_class
            k = (set_no * 3) % len(DOMAINS)
            domains = (DOMAINS + DOMAINS)[k:k + 5]
            jobs.append(batch(cls, domains, set_no))
    results = [p for chunk in await asyncio.gather(*jobs) for p in chunk]
    per_class: dict[str, list] = defaultdict(list)
    seen = set()
    for cls, p in results:
        key = (p["existing"].strip().lower(), p["new"].strip().lower())
        if key in seen or not p["existing"].strip() or not p["new"].strip():
            continue
        seen.add(key)
        per_class[cls].append(p)
    items = []
    for cls in RELATIONS:
        for p in per_class[cls][: args.per_class]:
            items.append({"existing": p["existing"].strip(), "new": p["new"].strip(), "label": cls})
    rng.shuffle(items)
    for i, it in enumerate(items):
        it["id"] = f"B{i:04d}"
    write_jsonl(DATA / "relation_pairs.jsonl", items)
    check = rng.sample(items, min(40, len(items)))
    with (DATA / "relation_label_check.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "existing", "new", "your_label (same|update|contradicts|narrows|unrelated)"])
        for it in check:
            w.writerow([it["id"], it["existing"], it["new"], ""])
    print(f"relation pairs: {len(items)} ({dict(Counter(i['label'] for i in items))}); "
          f"label check: {len(check)} rows -> {DATA / 'relation_label_check.csv'}")


# ── Questions and variants ──────────────────────────────────────────────────

GROUND_Q = {
    "base": ("Is the ATOM fully supported by the QUOTE? Answer true only if every claim in the ATOM "
             "(numbers, names, abbreviations, who did what, negation) is stated or directly implied by the QUOTE. "
             "The CONTEXT says who wrote the quote and when; the ATOM may use it for names and dates.",
             {"true": "The quote fully supports the atom.",
              "false": "The atom adds, changes or contradicts something in the quote."}),
    "wording": ("Does the QUOTE back up everything the ATOM says, including every number, name, code and "
                "who the claim is about? (Names and dates may come from the CONTEXT line.)",
                {"true": "Everything in the atom is backed by the quote.",
                 "false": "Something in the atom is not backed by the quote."}),
}
REL_WORDING = {"same": "duplicate", "update": "replaces", "contradicts": "conflicts",
               "narrows": "refines", "unrelated": "independent"}
REL_ALT_DESC = {
    "duplicate": "The new atom repeats the existing fact in other words.",
    "replaces": "The new atom supersedes the existing one with a newer value, state or correction.",
    "conflicts": "The two atoms cannot both be true and neither says it supersedes the other.",
    "refines": "The new atom adds a condition, exception or detail to the existing one; both stay true.",
    "independent": "The two atoms are separate facts that do not affect each other.",
}
REL_INSTR = {"base": "How does the NEW ATOM relate to the EXISTING ATOM?",
             "wording": "Classify the relationship of the NEW memory to the EXISTING memory."}


def grounding_question(item, variant):
    instr, crit = GROUND_Q["wording" if variant == "wording" else "base"]
    if variant == "order":
        crit = {"false": crit["false"], "true": crit["true"]}
    state = f"CONTEXT: {item['context']}\n\nQUOTE:\n{item['quote']}\n\nATOM:\n{item['atom']}"
    return state, {"type": "noul", "instructions": instr, "criteria": crit}


def relation_question(item, variant):
    keys = RELATIONS[:]
    if variant == "order":
        random.Random(item["id"]).shuffle(keys)
    if variant == "wording":
        crit = {REL_WORDING[k]: REL_ALT_DESC[REL_WORDING[k]] for k in keys}
    else:
        crit = {k: RELATION_SPEC[k] for k in keys}
    state = f"EXISTING ATOM:\n{item['existing']}\n\nNEW ATOM:\n{item['new']}"
    return state, {"type": "choice", "instructions": REL_INSTR["wording" if variant == "wording" else "base"],
                   "criteria": crit}


def unmap_relation(probs: dict, variant: str) -> dict:
    if variant != "wording":
        return {k: float(v) for k, v in probs.items()}
    back = {v: k for k, v in REL_WORDING.items()}
    return {back.get(k, k): float(v) for k, v in probs.items()}


# ── Running ─────────────────────────────────────────────────────────────────

async def ask_decision_model(http, model, state, question):
    body = {"model": model, "state": state, "questions": {"q": question}}
    headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"}
    t0 = time.monotonic()
    for attempt in range(3):
        try:
            r = await http.post(DECISIONS_URL, json=body, headers=headers)
        except Exception as exc:  # network
            err = type(exc).__name__
        else:
            if r.status_code == 200:
                data = r.json()
                ans = (data.get("answers") or {}).get("q") or {"type": "missing"}
                return ans, (data.get("usage") or {}).get("cost"), time.monotonic() - t0, None
            err = f"HTTP {r.status_code}"
            if r.status_code not in (408, 429, 500, 502, 503, 504):
                break
        await asyncio.sleep(1.5 * 2 ** attempt)
    return None, None, time.monotonic() - t0, err


GEN_GROUND_SCHEMA = {"type": "object", "required": ["answer", "probability_true"], "properties": {
    "answer": {"type": "boolean"}, "probability_true": {"type": "number", "minimum": 0, "maximum": 1}}}


async def ask_generative(client, model, state, question):
    """Same question, answered by a generative model as JSON (forced tool)."""
    if question["type"] == "noul":
        crit = question["criteria"]
        user = (f"{state}\n\nQuestion: {question['instructions']}\n"
                f"true: {crit['true']}\nfalse: {crit['false']}\n"
                "Give your answer and the probability that the answer is true.")
        schema = GEN_GROUND_SCHEMA
    else:
        keys = list(question["criteria"])
        user = (f"{state}\n\nQuestion: {question['instructions']}\nOptions:\n"
                + "\n".join(f"- {k}: {v}" for k, v in question["criteria"].items())
                + "\nGive the best option and a probability for every option (summing to 1).")
        schema = {"type": "object", "required": ["choice", "probabilities"], "properties": {
            "choice": {"enum": keys},
            "probabilities": {"type": "object", "required": keys,
                              "properties": {k: {"type": "number"} for k in keys}}}}
    t0 = time.monotonic()
    try:
        out, usage = await tool_call(client, model, "You answer classification questions about memory atoms.",
                                     user, "answer", schema, max_tokens=512)
    except Exception as exc:
        return None, None, time.monotonic() - t0, type(exc).__name__
    pin, pout = ANTHROPIC_PRICE.get(model, (0.0, 0.0))
    cost = (usage["input_tokens"] * pin + usage["output_tokens"] * pout) / 1e6
    if question["type"] == "noul":
        p = float(out.get("probability_true", 1.0 if out.get("answer") else 0.0))
        ans = {"type": "noul", "noul": min(max(p, 0.0), 1.0)}
    else:
        probs = {k: max(float(v), 0.0) for k, v in (out.get("probabilities") or {}).items()}
        s = sum(probs.values()) or 1.0
        ans = {"type": "choice", "choice": out.get("choice"), "probabilities": {k: v / s for k, v in probs.items()}}
    return ans, cost, time.monotonic() - t0, None


async def run(args) -> None:
    import httpx
    load_env_file(Path(args.env_file).expanduser())
    models = args.models.split(",") if args.models else DECISION_MODELS + [GENERATIVE_BASELINE]
    if any("/" in m for m in models) and not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("OPENROUTER_API_KEY not found (pass --env-file)")
    tasks = {"A": read_jsonl(CACHE / "grounding_items.jsonl"), "B": read_jsonl(DATA / "relation_pairs.jsonl")}
    if args.limit:
        tasks = {k: v[: args.limit] for k, v in tasks.items()}
    out_path = CACHE / "results.jsonl"
    done = {(r["model"], r["task"], r["item"], r["variant"]) for r in read_jsonl(out_path)}
    sem = asyncio.Semaphore(CONCURRENCY)
    client = anthropic_client()
    lock = asyncio.Lock()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    counter = Counter()

    async with httpx.AsyncClient(timeout=60.0) as http:
        async def one(model, task, item, variant):
            if (model, task, item["id"], variant) in done:
                return
            state, q = (grounding_question if task == "A" else relation_question)(item, variant)
            async with sem:
                if "/" in model:
                    ans, cost, latency, err = await ask_decision_model(http, model, state, q)
                else:
                    ans, cost, latency, err = await ask_generative(client, model, state, q)
            if ans and ans.get("type") == "choice" and ans.get("probabilities"):
                ans["probabilities"] = unmap_relation(ans["probabilities"], variant)
                ans["choice"] = max(ans["probabilities"], key=ans["probabilities"].get)
            # Results hold ids, labels and model outputs only — no item text.
            row = {"model": model, "task": task, "item": item["id"], "variant": variant,
                   "answer": ans, "cost": cost, "latency": round(latency, 4), "error": err}
            async with lock:
                with out_path.open("a") as f:
                    f.write(json.dumps(row) + "\n")
                counter[(model, err is None)] += 1

        jobs = [one(m, t, it, v) for m in models for t, items in tasks.items() for it in items for v in VARIANTS]
        print(f"{len(jobs)} requests ({len(done)} already done)")
        await asyncio.gather(*jobs)
    for (m, ok), n in sorted(counter.items()):
        print(f"  {m}: {'ok' if ok else 'error'} {n}")


# ── Metrics ─────────────────────────────────────────────────────────────────

def pct(x):
    return f"{100 * x:.1f}%"


def ece_and_table(conf_correct: list[tuple[float, bool]], bins: int = 10):
    table, ece, n = [], 0.0, len(conf_correct)
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(c, ok) for c, ok in conf_correct if (lo <= c < hi) or (b == bins - 1 and c == 1.0)]
        if not sel:
            continue
        mc = statistics.mean(c for c, _ in sel)
        acc = statistics.mean(1.0 if ok else 0.0 for _, ok in sel)
        ece += len(sel) / n * abs(mc - acc)
        table.append((f"{lo:.1f}-{hi:.1f}", len(sel), mc, acc))
    return ece, table


def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def report(args) -> None:
    rows = read_jsonl(CACHE / "results.jsonl")
    items = {"A": {i["id"]: i for i in read_jsonl(CACHE / "grounding_items.jsonl")},
             "B": {i["id"]: i for i in read_jsonl(DATA / "relation_pairs.jsonl")}}
    by = defaultdict(dict)   # (model, task) -> {(item, variant): row}
    for r in rows:
        by[(r["model"], r["task"])][(r["item"], r["variant"])] = r
    models = sorted({m for m, _ in by})
    print(f"_Generated {datetime.now():%Y-%m-%d %H:%M} from {len(rows)} results._\n")

    for task in ("A", "B"):
        title = "Task A — grounding (noul: is the atom fully supported by the quote?)" if task == "A" \
            else "Task B — relation (choice: same / update / contradicts / narrows / unrelated)"
        print(f"### {title}\n")
        n_items = len(items[task])
        if task == "A":
            print("| model | n | answered | accuracy | recall supported | recall unsupported | Brier | ECE | flip: order | flip: wording | mean abs Δp (order / wording) | p50 s | p95 s | $ / 1k |")
            print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        else:
            print("| model | n | answered | accuracy | " + " | ".join(f"recall {c}" for c in RELATIONS)
                  + " | Brier | ECE | flip: order | flip: wording | p50 s | p95 s | $ / 1k |")
            print("|---|---|---|---|" + "---|" * len(RELATIONS) + "---|---|---|---|---|---|---|")
        reliab = {}
        extra = {}
        for m in models:
            res = by.get((m, task))
            if not res:
                continue
            base = {i: r for (i, v), r in res.items() if v == "base"}
            answered, correct, brier, cc = 0, 0, [], []
            per_class = defaultdict(lambda: [0, 0])
            per_pert = defaultdict(lambda: [0, 0])
            pred = {}
            for iid, r in base.items():
                it = items[task].get(iid)
                if not it:
                    continue
                a = r["answer"] or {}
                if task == "A":
                    y = it["label"]
                    if a.get("type") != "noul":
                        per_class[y][1] += 1
                        if not y:
                            per_pert[it["perturbation"]][1] += 1
                        continue
                    p = float(a["noul"])
                    yhat = p >= 0.5
                    ok = yhat == y
                    brier.append((p - (1.0 if y else 0.0)) ** 2)
                    cc.append((max(p, 1 - p), ok))
                    pred[iid] = (yhat, p)
                    per_class[y][0] += ok; per_class[y][1] += 1
                    if not y:
                        per_pert[it["perturbation"]][0] += ok; per_pert[it["perturbation"]][1] += 1
                else:
                    y = it["label"]
                    probs = a.get("probabilities") or {}
                    if a.get("type") != "choice" or not probs:
                        per_class[y][1] += 1
                        continue
                    yhat = max(probs, key=probs.get)
                    ok = yhat == y
                    brier.append(sum((probs.get(c, 0.0) - (1.0 if c == y else 0.0)) ** 2 for c in RELATIONS))
                    cc.append((probs[yhat], ok))
                    pred[iid] = (yhat, probs)
                    per_class[y][0] += ok; per_class[y][1] += 1
                answered += 1
                correct += ok
            total = len([i for i in base if i in items[task]])
            flips, dps = {}, {}
            for var in ("order", "wording"):
                f, d, n = 0, [], 0
                for (iid, v), r in res.items():
                    if v != var or iid not in pred:
                        continue
                    a = r["answer"] or {}
                    if task == "A" and a.get("type") == "noul":
                        p2 = float(a["noul"]); n += 1
                        f += (p2 >= 0.5) != pred[iid][0]; d.append(abs(p2 - pred[iid][1]))
                    elif task == "B" and a.get("type") == "choice" and a.get("probabilities"):
                        n += 1
                        f += max(a["probabilities"], key=a["probabilities"].get) != pred[iid][0]
                flips[var] = f / n if n else float("nan")
                dps[var] = statistics.mean(d) if d else float("nan")
            lat = [r["latency"] for r in res.values() if r["error"] is None]
            costs = [r["cost"] for r in res.values() if r.get("cost") is not None]
            cost_k = 1000 * statistics.mean(costs) if costs else float("nan")
            ece, table = ece_and_table(cc)
            reliab[m] = table
            acc = correct / answered if answered else float("nan")
            rec = lambda k: (per_class[k][0] / per_class[k][1]) if per_class[k][1] else float("nan")
            if task == "A":
                print(f"| {m} | {total} | {pct(answered/total) if total else '-'} | {pct(acc)} | {pct(rec(True))} | "
                      f"{pct(rec(False))} | {statistics.mean(brier):.3f} | {ece:.3f} | {pct(flips['order'])} | "
                      f"{pct(flips['wording'])} | {dps['order']:.3f} / {dps['wording']:.3f} | "
                      f"{quantile(lat, .5):.2f} | {quantile(lat, .95):.2f} | {cost_k:.4f} |")
                extra[m] = {k: (v[0] / v[1] if v[1] else float('nan'), v[1]) for k, v in per_pert.items()}
            else:
                print(f"| {m} | {total} | {pct(answered/total) if total else '-'} | {pct(acc)} | "
                      + " | ".join(pct(rec(c)) for c in RELATIONS)
                      + f" | {statistics.mean(brier) if brier else float('nan'):.3f} | {ece:.3f} | "
                      f"{pct(flips['order'])} | {pct(flips['wording'])} | {quantile(lat, .5):.2f} | "
                      f"{quantile(lat, .95):.2f} | {cost_k:.4f} |")
        print(f"\n{n_items} items × {len(VARIANTS)} variants. Accuracy, recall, Brier and ECE are on the base "
              "variant; unanswered items (refusal, missing answer, error) count against recall and the "
              "answered rate, not accuracy. Flip = share of answered items whose predicted label changes "
              "under the variant.\n")
        if task == "A" and extra:
            print("Recall on unsupported items, by perturbation:\n")
            print("| model | " + " | ".join(PERTURBATIONS) + " |")
            print("|---|" + "---|" * len(PERTURBATIONS))
            for m, d in extra.items():
                print(f"| {m} | " + " | ".join(
                    f"{pct(d[k][0])} (n={d[k][1]})" if k in d else "-" for k in PERTURBATIONS) + " |")
            print()
        print("<details><summary>Reliability tables (confidence of the predicted label vs. accuracy)</summary>\n")
        for m, table in reliab.items():
            print(f"**{m}**\n\n| bin | n | mean confidence | accuracy |\n|---|---|---|---|")
            for b, n, mc, acc in table:
                print(f"| {b} | {n} | {mc:.3f} | {acc:.3f} |")
            print()
        print("</details>\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("build-grounding")
    g.add_argument("--locomo", type=int, default=24, help="LoCoMo passages from mnemo_eval")
    g.add_argument("--max-positives", type=int, default=160)
    g.add_argument("--seed", type=int, default=7)
    r = sub.add_parser("build-relation")
    r.add_argument("--per-class", type=int, default=40)
    r.add_argument("--seed", type=int, default=7)
    x = sub.add_parser("run")
    x.add_argument("--env-file", default="~/.config/mnemo/openrouter.env")
    x.add_argument("--models", default="", help="comma-separated; default: all")
    x.add_argument("--limit", type=int, default=0, help="first N items per task (pilot)")
    sub.add_parser("report")
    args = ap.parse_args()
    if args.cmd == "build-grounding":
        asyncio.run(build_grounding(args))
    elif args.cmd == "build-relation":
        asyncio.run(build_relation(args))
    elif args.cmd == "run":
        asyncio.run(run(args))
    else:
        report(args)


if __name__ == "__main__":
    main()
