"""Phase 2 kind bake-off (docs/phase2-spec.md item 4; docs/phase2-design.md §4).

About 150 SYNTHETIC atoms, balanced across state / plan / observation / rule,
including the hard boundaries (baseline vs reading, booked vs attended, habit
vs one-off, "currently uses" vs "always use"). Written by Haiku 5.5 through
Anthropic with invented names. No production text goes to any provider.

Models: typesafe/jev-1.14 and liquid/d1.2-ultra (OpenRouter Decisions, every
request with provider {"zdr": true}, through mnemo.server.decisions), and
claude-haiku-5-5 as a generative baseline. Variants: base, order (options
reversed), wording (instructions and option names reworded, mapped back).

    uv run python scripts/eval/kind_bakeoff.py generate      # -> scripts/eval/data/kind_atoms.jsonl
    uv run python scripts/eval/kind_bakeoff.py run            # -> ~/.cache/mnemo-bakeoff/kind_results.jsonl
    uv run python scripts/eval/kind_bakeoff.py report         # markdown to stdout
    uv run python scripts/eval/kind_bakeoff.py labelcheck     # -> scripts/eval/data/kind_label_check.csv

Keys: OPENROUTER_API_KEY from --env-file (default ~/.config/mnemo/openrouter.env),
ANTHROPIC_API_KEY from the environment. Neither is printed. Results hold ids,
labels and model outputs only.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_bakeoff as db  # noqa: E402  (sets MNEMO_* env defaults before mnemo.server.config loads)

from mnemo.server.decisions import choice  # noqa: E402
from mnemo.server.decisions.openrouter import OpenRouterDecisions  # noqa: E402
from mnemo.server.decisions.questions import KIND_CRITERIA, KIND_INSTRUCTIONS, KINDS, atom_state  # noqa: E402

ITEMS = db.DATA / "kind_atoms.jsonl"
RESULTS = db.CACHE / "kind_results.jsonl"
LABEL_CHECK = db.DATA / "kind_label_check.csv"
DECISION_MODELS = ["typesafe/jev-1.14", "liquid/d1.2-ultra"]
GENERATIVE = ["claude-haiku-5-5"]
GENERATOR = "claude-haiku-5-5"
VARIANTS = ["base", "order", "wording"]
KIND_THRESHOLD = 0.80   # provisional (Tom, 2026-11-24)

BUCKETS = {
    "state": {
        "status": "the current status or phase of a project, task or system (e.g. mid-way, paused, blocked)",
        "value": "a current value, setting or configuration (a limit, a version in use, a price currently charged)",
        "baseline": "a baseline or typical level that holds until it changes (resting heart rate baseline, normal load)",
        "uses": "what something currently uses, where something currently is, or who currently owns something",
    },
    "plan": {
        "booking": "a booked or scheduled appointment, meeting or trip with a future date",
        "intention": "an intention or decision to do something later, with or without a date",
        "conditional": "a plan that depends on something else happening first ('after the review we will ...')",
        "expectation": "an expected future event or deadline that has not happened yet",
    },
    "observation": {
        "reading": "a single dated measurement or reading (one value taken at one time)",
        "event": "something that happened on a particular day (a meeting held, a release shipped)",
        "action": "an action someone took at a particular time (sent, fixed, paid, cancelled)",
        "attended": "a booked thing that actually took place, reported after the fact",
    },
    "rule": {
        "preference": "a lasting preference of a person, not tied to a time",
        "procedure": "a procedure or requirement that always applies ('deploys need ...')",
        "fact": "a general fact or definition that does not change with time",
        "habit": "a habit or recurring practice ('usually', 'every Monday', 'always')",
    },
}
PER_BUCKET = 10   # 4 kinds x 4 buckets x 10 = 160, deduplicated to about 150

GEN_SYSTEM = ("You write synthetic test data for a memory system. Every item is ONE standalone memory atom "
              "(one sentence) that names its subject, uses invented names only (no real people, companies or "
              "products), and belongs unambiguously to the requested category. Vary wording and domains. Do not "
              "make the category obvious from stock words alone.")
GEN_SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["text", "note_date", "event_date", "date_precision"],
    "properties": {
        "text": {"type": "string"},
        "note_date": {"type": "string", "description": "YYYY-MM-DD in 2026, the day the note was written"},
        "event_date": {"type": ["string", "null"], "description": "YYYY-MM-DD the atom's date refers to, or null"},
        "date_precision": {"enum": ["day", "month", "year", "unknown"]},
    }}}}}

# Reworded variant: different instructions and option names, mapped back to the kinds.
WORDING_KEYS = {"state": "condition", "plan": "intention", "observation": "event", "rule": "principle"}
WORDING_INSTR = "Classify the MEMORY in the ATOM line by what sort of statement it is. Use the CONTEXT only for names and dates."
WORDING_CRITERIA = {
    "condition": "Describes how something currently is (a status, setting, level or arrangement) until that changes.",
    "intention": "Describes something meant, booked or expected to happen in the future.",
    "event": "Reports one thing that took place or one measurement taken at a specific time.",
    "principle": "States something generally true, a preference, a habit or a procedure, independent of any particular time.",
}


def question(variant: str) -> dict:
    if variant == "wording":
        return choice(WORDING_INSTR, WORDING_CRITERIA)
    crit = dict(KIND_CRITERIA)
    if variant == "order":
        crit = dict(reversed(list(crit.items())))
    return choice(KIND_INSTRUCTIONS, crit)


def unmap(probs: dict, variant: str) -> dict:
    if variant != "wording":
        return {k: float(v) for k, v in probs.items()}
    back = {v: k for k, v in WORDING_KEYS.items()}
    return {back.get(k, k): float(v) for k, v in probs.items()}


def state_for(item: dict) -> str:
    from datetime import datetime
    def d(s):
        return datetime.fromisoformat(s) if s else None
    return atom_state(item["text"], item["text"], "the user", None, d(item["note_date"]), d(item["note_date"]),
                      d(item["event_date"]), item["date_precision"])


# ── generate ────────────────────────────────────────────────────────────────

async def generate(args) -> None:
    rng = random.Random(args.seed)
    client = db.anthropic_client()
    sem = asyncio.Semaphore(4)
    out: list[dict] = []

    async def one(kind, bucket, desc):
        domains = ", ".join(rng.sample(db.DOMAINS, 4))
        user = (f"Category: {kind} — {desc}.\nDomains to spread across: {domains}.\n"
                f"Write {PER_BUCKET} different items. Notes are written during 2026.")
        async with sem:
            data, _ = await db.tool_call(client, GENERATOR, GEN_SYSTEM, user, "record_items", GEN_SCHEMA, max_tokens=3000)
        for it in data.get("items", [])[:PER_BUCKET]:
            out.append({"kind": kind, "bucket": f"{kind}:{bucket}", **{k: it.get(k) for k in GEN_SCHEMA["properties"]["items"]["items"]["properties"]}})

    await asyncio.gather(*(one(k, b, d) for k, bs in BUCKETS.items() for b, d in bs.items()))
    seen, items = set(), []
    for it in sorted(out, key=lambda x: (x["bucket"], x["text"])):
        key = it["text"].strip().lower()
        if key in seen or not it["text"].strip():
            continue
        seen.add(key)
        items.append(it)
    rng.shuffle(items)
    for i, it in enumerate(items):
        it["id"] = f"K{i:04d}"
    db.write_jsonl(ITEMS, items)
    print(f"{len(items)} items -> {ITEMS.relative_to(db.REPO)}")
    for k, n in sorted(Counter(it["kind"] for it in items).items()):
        print(f"  {k}: {n}")


# ── run ─────────────────────────────────────────────────────────────────────

async def run(args) -> None:
    db.load_env_file(Path(args.env_file).expanduser())
    items = db.read_jsonl(ITEMS)
    if args.limit:
        items = items[: args.limit]
    models = args.models.split(",") if args.models else DECISION_MODELS + GENERATIVE
    done = {(r["model"], r["item"], r["variant"]) for r in db.read_jsonl(RESULTS)}
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(db.CONCURRENCY)
    lock = asyncio.Lock()
    client = db.anthropic_client()
    clients = {m: OpenRouterDecisions(model=m, fallback_model="", timeout=30.0) for m in models if "/" in m}
    counter = Counter()

    async def one(model, item, variant):
        if (model, item["id"], variant) in done:
            return
        state, q = state_for(item), question(variant)
        async with sem:
            if "/" in model:
                res = await clients[model].ask(state, {"kind": q})
                ans = (res.answers or {}).get("kind") if res.ok else None
                cost, latency, err = res.cost, res.latency_ms / 1000, (None if res.ok else res.error)
                if res.ok and not ans:
                    err = "missing answer"
            else:
                ans, cost, latency, err = await db.ask_generative(client, model, state, q)
        if ans and ans.get("type") == "choice" and ans.get("probabilities"):
            ans["probabilities"] = unmap(ans["probabilities"], variant)
            ans["choice"] = max(ans["probabilities"], key=ans["probabilities"].get)
        row = {"model": model, "item": item["id"], "variant": variant, "answer": ans,
               "cost": cost, "latency": round(latency, 4), "error": err}
        async with lock:
            with RESULTS.open("a") as f:
                f.write(json.dumps(row) + "\n")
            counter[(model, err is None)] += 1

    jobs = [one(m, it, v) for m in models for it in items for v in VARIANTS]
    print(f"{len(jobs)} requests ({len(done)} already done)")
    t0 = time.monotonic()
    await asyncio.gather(*jobs)
    print(f"done in {time.monotonic() - t0:.0f} s")
    for (m, ok), n in sorted(counter.items()):
        print(f"  {m}: {'ok' if ok else 'error'} {n}")


# ── report ──────────────────────────────────────────────────────────────────

def _q(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


def hand_labels() -> dict[str, str]:
    """Tom's labels from kind_label_check.csv (blank rows ignored)."""
    if not LABEL_CHECK.exists():
        return {}
    out = {}
    with LABEL_CHECK.open() as f:
        for row in list(csv.reader(f))[1:]:
            lab = row[-1].strip().lower() if row else ""
            if lab in KINDS:
                out[row[0]] = lab
    return out


def threshold_table(items, by, models) -> None:
    print("\n## Kind threshold: coverage, accuracy and wrong kinds kept (base variant)\n")
    print("| threshold | " + " | ".join(f"{m}: kept / accuracy / wrong" for m in models) + " |")
    print("|---|" + "---|" * len(models))
    for t in (0.5, 0.6, 0.7, 0.8, 0.9):
        cells = []
        for m in models:
            rs = [r for r in by[(m, "base")].values() if r.get("answer") and r["answer"].get("probabilities")]
            keep = [r for r in rs if max(r["answer"]["probabilities"].values()) >= t]
            ok = sum(r["answer"]["choice"] == items[r["item"]]["kind"] for r in keep)
            cells.append(f"{db.pct(len(keep) / len(items))} / {db.pct(ok / max(1, len(keep)))} / {len(keep) - ok}")
        print(f"| {t:.1f} | " + " | ".join(cells) + " |")


def report(args) -> None:
    items = {it["id"]: dict(it) for it in db.read_jsonl(ITEMS)}
    hand = hand_labels() if not args.generated_labels else {}
    changed = sum(items[i]["kind"] != lab for i, lab in hand.items() if i in items)
    for i, lab in hand.items():
        if i in items:
            items[i]["kind"] = lab
    rows = db.read_jsonl(RESULTS)
    by = defaultdict(dict)                      # (model, variant) -> item -> row
    for r in rows:
        by[(r["model"], r["variant"])][r["item"]] = r
    models = [m for m in DECISION_MODELS + GENERATIVE if (m, "base") in by]
    n_items = len(items)
    print(f"# Kind bake-off results\n\n{n_items} synthetic atoms: " +
          ", ".join(f"{k} {v}" for k, v in sorted(Counter(i['kind'] for i in items.values()).items())) +
          f". Variants: {', '.join(VARIANTS)}. Kind threshold for coverage: {KIND_THRESHOLD}.\n")
    if hand:
        print(f"Labels: Tom's hand labels for the {len(hand)} checked items ({changed} differ from the generated "
              f"label); generated labels for the other {n_items - len(hand)}.\n")
    else:
        print("Labels: generated labels only.\n")
    print("| model | answered | accuracy | " + " | ".join(f"recall {k}" for k in KINDS) +
          " | Brier | ECE | flip: order | flip: wording | coverage ≥ thr | accuracy ≥ thr | p50 s | p95 s | $ / 1k |")
    print("|---|---|---|" + "---|" * len(KINDS) + "---|---|---|---|---|---|---|---|---|")
    preds, tables = {}, {}
    for m in models:
        base = by[(m, "base")]
        ans = {i: r["answer"] for i, r in base.items() if r.get("answer") and r["answer"].get("probabilities")}
        pred = {i: a["choice"] for i, a in ans.items()}
        preds[m] = pred
        correct = [pred[i] == items[i]["kind"] for i in pred]
        rec = {k: statistics.mean([pred.get(i) == k for i, it in items.items() if it["kind"] == k] or [0]) for k in KINDS}
        brier = statistics.mean(sum((a["probabilities"].get(k, 0.0) - (1.0 if items[i]["kind"] == k else 0.0)) ** 2
                                    for k in KINDS) for i, a in ans.items()) if ans else float("nan")
        conf = [(max(a["probabilities"].values()), pred[i] == items[i]["kind"]) for i, a in ans.items()]
        ece, table = db.ece_and_table(conf)
        tables[m] = table
        flips = {}
        for v in ("order", "wording"):
            other = by[(m, v)]
            both = [i for i in pred if (other.get(i) or {}).get("answer") and other[i]["answer"].get("choice")]
            flips[v] = statistics.mean([other[i]["answer"]["choice"] != pred[i] for i in both]) if both else float("nan")
        cov = [(c, ok) for c, ok in conf if c >= KIND_THRESHOLD]
        lat = [r["latency"] for v in VARIANTS for r in by[(m, v)].values() if r.get("error") is None]
        costs = [r["cost"] for v in VARIANTS for r in by[(m, v)].values() if r.get("cost") is not None]
        print(f"| {m} | {db.pct(len(ans) / n_items)} | {db.pct(statistics.mean(correct)) if correct else '-'} | "
              + " | ".join(db.pct(rec[k]) for k in KINDS)
              + f" | {brier:.3f} | {ece:.3f} | {db.pct(flips['order'])} | {db.pct(flips['wording'])} | "
              f"{db.pct(len(cov) / max(1, len(conf)))} | {db.pct(statistics.mean(ok for _, ok in cov)) if cov else '-'} | "
              f"{_q(lat, 0.5):.2f} | {_q(lat, 0.95):.2f} | "
              + (f"{1000 * statistics.mean(costs):.4f} |" if costs else "- |"))
    print("\nAnswered = base-variant items with a usable answer. Accuracy, recall, Brier and ECE are on the base "
          "variant. Flip = share of items whose label changes under the variant. Coverage = share of answered "
          f"items whose top probability is at least {KIND_THRESHOLD} (the rest would be stored as `unknown`).\n")

    if models:
        best = max(models, key=lambda m: statistics.mean([preds[m][i] == items[i]["kind"] for i in preds[m]] or [0]))
        print(f"## Paired comparison against the most accurate model, `{best}`\n")
        print("| model | b (best right, other wrong) | c (reverse) | exact p |\n|---|---|---|---|")
        for m in models:
            if m == best:
                continue
            common = set(preds[m]) & set(preds[best])
            b = sum(preds[best][i] == items[i]["kind"] and preds[m][i] != items[i]["kind"] for i in common)
            c = sum(preds[m][i] == items[i]["kind"] and preds[best][i] != items[i]["kind"] for i in common)
            print(f"| {m} | {b} | {c} | {db.mcnemar_exact(b, c):.3f} |")

    threshold_table(items, by, models)

    print("\n## Accuracy by boundary bucket (base variant)\n")
    buckets = sorted({it["bucket"] for it in items.values()})
    print("| bucket | n | " + " | ".join(models) + " |\n|---|---|" + "---|" * len(models))
    for bkt in buckets:
        ids = [i for i, it in items.items() if it["bucket"] == bkt]
        print(f"| {bkt} | {len(ids)} | " + " | ".join(
            db.pct(statistics.mean([preds[m].get(i) == items[i]["kind"] for i in ids])) for m in models) + " |")

    for m in models:
        print(f"\n## `{m}`: confusion (rows = generated label, columns = predicted) and reliability\n")
        print("| label \\ predicted | " + " | ".join(KINDS) + " | no answer |\n|---|" + "---|" * (len(KINDS) + 1))
        for k in KINDS:
            ids = [i for i, it in items.items() if it["kind"] == k]
            print(f"| {k} | " + " | ".join(str(sum(preds[m].get(i) == p for i in ids)) for p in KINDS)
                  + f" | {sum(i not in preds[m] for i in ids)} |")
        print("\n| top probability | n | mean confidence | accuracy |\n|---|---|---|---|")
        for rng_, n, mc, acc in tables[m]:
            print(f"| {rng_} | {n} | {mc:.2f} | {db.pct(acc)} |")


# ── label check ─────────────────────────────────────────────────────────────

def labelcheck(args) -> None:
    items = {it["id"]: it for it in db.read_jsonl(ITEMS)}
    pred = defaultdict(dict)
    for r in db.read_jsonl(RESULTS):
        if r["variant"] == "base" and r.get("answer") and r["answer"].get("choice"):
            pred[r["item"]][r["model"]] = r["answer"]["choice"]
    disagree = [i for i, it in items.items() if sum(p != it["kind"] for p in pred[i].values()) >= args.min_disagree]
    rng = random.Random(args.seed)
    rest = [i for i in items if i not in disagree]
    pick = sorted(disagree) + sorted(rng.sample(rest, min(args.random_n, len(rest))))
    rng.shuffle(pick)
    with LABEL_CHECK.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "text", "note_date", "event_date", "your_label (state|plan|observation|rule)"])
        for i in pick:
            it = items[i]
            w.writerow([i, it["text"], it["note_date"], it["event_date"] or "", ""])
    print(f"{len(pick)} rows ({len(disagree)} disagreements + {len(pick) - len(disagree)} random) -> "
          f"{LABEL_CHECK.relative_to(db.REPO)}; generated labels are not in the file")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--seed", type=int, default=13)
    r = sub.add_parser("run")
    r.add_argument("--env-file", default="~/.config/mnemo/openrouter.env")
    r.add_argument("--models", default="")
    r.add_argument("--limit", type=int, default=0)
    rp = sub.add_parser("report")
    rp.add_argument("--generated-labels", action="store_true", help="ignore Tom's hand labels")
    lc = sub.add_parser("labelcheck")
    lc.add_argument("--min-disagree", type=int, default=2)
    lc.add_argument("--random-n", type=int, default=20)
    lc.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    if args.cmd == "generate":
        asyncio.run(generate(args))
    elif args.cmd == "run":
        asyncio.run(run(args))
    elif args.cmd == "report":
        report(args)
    else:
        labelcheck(args)


if __name__ == "__main__":
    main()
