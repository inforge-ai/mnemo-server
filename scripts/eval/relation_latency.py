"""remember latency with and without the relation check (v2 Phase 2, before G2).

Runs the real store path in-process (store_service.create_store + run_store)
against the TEST database, with the FAKE embedder (memory on this host), the live decomposer
(Anthropic) and the live decision model (OpenRouter, provider {"zdr": true}).
Synthetic text only: the notes are built from scripts/eval/data/kind_atoms.jsonl.

Per configuration, a fresh scratch agent is seeded with the same notes (so
candidates exist), then the measured notes are stored and timed:
    off                            relations off (today's store path)
    shadow k=5                     the relation check, k = 5 candidates per atom
    shadow k=5, k=3 over 20 atoms  k = 3 for stores of more than 20 atoms
Measured notes: 5 of 11 sentences (production p50 is 11 atoms per store) and
2 of 29 sentences (production p95). Prints JSON lines and a markdown table.

    MNEMO_ENV_FILE=.env.test ANTHROPIC_API_KEY=... OPENROUTER_API_KEY=... \\
        uv run python scripts/eval/relation_latency.py
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.environ.setdefault("MNEMO_ENV_FILE", ".env.test")
os.environ.setdefault("MNEMO_EMBEDDING_BACKEND", "fake")   # Tom, 2026-11-24: fake embedder (memory)
os.environ.setdefault("MNEMO_DECOMPOSER_PROVIDER", "anthropic")
os.environ.setdefault("MNEMO_DECOMPOSER_MODEL", "claude-haiku-5-5")
os.environ.setdefault("MNEMO_DECISION_PROVIDER", "openrouter")

from mnemo.server.config import settings  # noqa: E402

if "test" not in settings.database_url:
    raise SystemExit("refusing: relation_latency runs only against a test database")

ITEMS = REPO / "scripts" / "eval" / "data" / "kind_atoms.jsonl"
SEED_NOTES, SEED_SIZE = 10, 3
MEDIUM = (5, 11)
LARGE = (2, 29)
CONFIGS = [("off", "off", 5, None), ("shadow k=5", "shadow", 5, None), ("shadow k=5, k=3 over 20 atoms", "shadow", 5, 3)]


def q(xs, p):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def notes():
    items = [json.loads(line) for line in ITEMS.read_text().splitlines() if line.strip()]
    texts = [it["text"] for it in items]
    i = 0
    seeds, measured = [], []
    for _ in range(SEED_NOTES):
        seeds.append(" ".join(texts[i:i + SEED_SIZE]))
        i += SEED_SIZE
    for count, size in (MEDIUM, LARGE):
        for _ in range(count):
            measured.append((size, " ".join(texts[i:i + size])))
            i += size
    return seeds, measured


async def main() -> None:
    from mnemo.server.database import close_pool, create_pool, set_pool
    from mnemo.server.embeddings import warmup
    from mnemo.server.services import store_service

    warmup()
    pool = await create_pool()
    set_pool(pool)
    seeds, measured = notes()
    results = []
    try:
        only = [int(x) for x in os.environ.get("MNEMO_LAT_CONFIGS", "").split(",") if x]
        for idx, (label, mode, k, k_large) in enumerate(CONFIGS):
            if only and idx not in only:
                continue
            settings.relation_mode = mode
            settings.relation_k = k
            settings.relation_k_large = k_large
            async with pool.acquire() as conn:
                tag = uuid.uuid4().hex[:6]
                op = await conn.fetchval(
                    "INSERT INTO operators (name, username, email) VALUES ($1, $2, $3) RETURNING id",
                    f"latency-{tag}", f"lat{tag}", f"lat-{tag}@test.invalid")
                agent = await conn.fetchval("INSERT INTO agents (operator_id, name) VALUES ($1, $2) RETURNING id",
                                            op, f"lat-{uuid.uuid4().hex[:6]}")

            async def store(text, mode_now):
                settings.relation_mode = mode_now
                async with pool.acquire() as conn:
                    src = await store_service.create_store(
                        conn, agent_id=agent, operator_id=op, agent_name="lat", agent_metadata=None, text=text,
                        domain_tags=[], remembered_on=None, speaker=None, addressee=None, channel=None,
                        origin_class="unspecified")
                t0 = time.monotonic()
                out = await store_service.run_store(pool, src)
                return time.monotonic() - t0, out

            for s in seeds:                                   # seeding: relations off, not timed
                await store(s, "off")
            for size, text in measured:
                wall, out = await store(text, mode)
                t = out.get("timings_ms") or {}
                rel_ms = sum(v for key, v in t.items() if key.startswith("relations_") and key != "relations_k")
                async with pool.acquire() as conn:
                    pending = await conn.fetchval(
                        "SELECT count(*) FROM relation_decisions d JOIN sources s ON s.id = d.source_id "
                        "WHERE s.store_id = $1 AND d.final_relation = 'pending'", out["store_id"])
                    pairs = await conn.fetchval(
                        "SELECT count(*) FROM relation_decisions d JOIN sources s ON s.id = d.source_id "
                        "WHERE s.store_id = $1", out["store_id"])
                row = {"config": label, "sentences": size, "status": out["status"],
                       "atoms": len(out.get("atoms") or []), "wall_s": round(wall, 3),
                       "relations_s": round(rel_ms / 1000, 3), "pairs": pairs, "pending_pairs": pending,
                       "budget_exhausted": pending > 0, "timings_ms": t}
                results.append(row)
                print(json.dumps(row), flush=True)
            async with pool.acquire() as conn:           # clean up the scratch agent
                await conn.execute("DELETE FROM relation_decisions WHERE agent_id = $1", agent)
                await conn.execute("DELETE FROM conflicts WHERE agent_id = $1", agent)
                await conn.execute("DELETE FROM edges WHERE source_id IN (SELECT id FROM atoms WHERE agent_id = $1)"
                                   " OR target_id IN (SELECT id FROM atoms WHERE agent_id = $1)", agent)
                await conn.execute("DELETE FROM source_merges WHERE atom_id IN (SELECT id FROM atoms WHERE agent_id = $1)", agent)
                await conn.execute("DELETE FROM atoms WHERE agent_id = $1", agent)
                await conn.execute("DELETE FROM decomposer_usage WHERE agent_id = $1", agent)
                await conn.execute("DELETE FROM store_jobs WHERE agent_id = $1", agent)
                await conn.execute("DELETE FROM sources WHERE agent_id = $1", agent)
                await conn.execute("DELETE FROM agents WHERE id = $1", agent)
                await conn.execute("DELETE FROM operators WHERE id = $1", op)
    finally:
        await close_pool()

    print("\n| config | stores | atoms p50 / max | remember p50 | p95 | max | relation step p50 | p95 | stores over budget | pending pairs |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for label, *_ in CONFIGS:
        rs = [r for r in results if r["config"] == label]
        if not rs:
            continue
        walls = [r["wall_s"] for r in rs]
        rels = [r["relations_s"] for r in rs]
        print(f"| {label} | {len(rs)} | {q([r['atoms'] for r in rs], .5):.0f} / {max(r['atoms'] for r in rs)} | "
              f"{q(walls, .5):.2f} s | {q(walls, .95):.2f} s | {max(walls):.2f} s | "
              f"{q(rels, .5):.2f} s | {q(rels, .95):.2f} s | {sum(r['budget_exhausted'] for r in rs)} | "
              f"{sum(r['pending_pairs'] for r in rs)} / {sum(r['pairs'] for r in rs)} |")
    for size in (MEDIUM[1], LARGE[1]):
        print(f"\nBy note size {size} sentences:")
        for label, *_ in CONFIGS:
            rs = [r for r in results if r["config"] == label and r["sentences"] == size]
            walls = ", ".join(f"{r['wall_s']:.2f}" for r in rs)
            print(f"  {label}: wall {walls} s; atoms {[r['atoms'] for r in rs]}; "
                  f"over budget {sum(r['budget_exhausted'] for r in rs)}")


if __name__ == "__main__":
    asyncio.run(main())
