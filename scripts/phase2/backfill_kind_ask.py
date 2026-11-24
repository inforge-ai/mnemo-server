"""Backfill 8.3, step 2 of 3: ask the kind question for planned atoms.

Runs INSIDE the server container (production decision client and key; every
request with provider {"zdr": true}):

    docker exec -i mnemo-server-mnemo-server-1 sh -c 'cd /app && .venv/bin/python -' \\
        < scripts/phase2/backfill_kind_ask.py

Reads repair_kind_2026_12 rows not yet asked, asks the production `kind`
question (decisions/questions.py) on the production per-atom state, and fills
new_kind (the top kind if its probability >= MNEMO_KIND_THRESHOLD, else
'unknown'), new_kind_p, kind_top, model, cost. Atoms are not touched; step 3
applies. Safe to re-run: only unasked rows are picked. Prints counts only.
"""

import asyncio
import json

import asyncpg

from mnemo.server.config import settings
from mnemo.server.decisions import get_decision_model
from mnemo.server.decisions.questions import KINDS, atom_state, kind_question
from mnemo.server.services.relation_service import legacy_dates

CONCURRENCY = 8


async def main() -> None:
    model = get_decision_model()
    if model is None:
        raise SystemExit("MNEMO_DECISION_PROVIDER is off")
    conn = await asyncpg.connect(settings.database_url)
    try:
        rows = await conn.fetch(
            """
            SELECT u.atom_id, a.text_content, a.source_quote, a.remembered_on, a.created_at,
                   a.valid_from, a.date_precision, a.date_basis, s.speaker
            FROM repair_kind_2026_12 u JOIN atoms a ON a.id = u.atom_id LEFT JOIN sources s ON s.id = a.source_id
            WHERE u.asked_at IS NULL AND u.reverted_at IS NULL
            ORDER BY u.atom_id
            """)
        sem = asyncio.Semaphore(CONCURRENCY)
        counts = {"to_ask": len(rows), "answered": 0, "errors": 0}
        kinds: dict[str, int] = {}
        cost = 0.0
        lock = asyncio.Lock()

        async def one(r):
            nonlocal cost
            if r["date_basis"]:
                vf, prec, basis = r["valid_from"] or r["created_at"], r["date_precision"], r["date_basis"]
            else:
                vf, prec, basis = legacy_dates(r["text_content"], r["remembered_on"], r["created_at"])
            state = atom_state(r["text_content"], r["source_quote"], r["speaker"], None, r["remembered_on"],
                               r["created_at"], vf if basis == "event" else None, prec)
            async with sem:
                res = await model.ask(state, {"kind": kind_question()})
            probs = res.probabilities("kind") if res.ok else None
            async with lock:
                if not probs:
                    counts["errors"] += 1
                    await conn.execute("UPDATE repair_kind_2026_12 SET error = $2 WHERE atom_id = $1",
                                       r["atom_id"], (res.error or "no answer")[:200])
                    return
                top = max(probs, key=probs.get)
                p = float(probs[top])
                kind = top if top in KINDS and p >= settings.kind_threshold else "unknown"
                await conn.execute(
                    "UPDATE repair_kind_2026_12 SET new_kind = $2, new_kind_p = $3, kind_top = $4, model = $5, "
                    "cost = $6, error = NULL, asked_at = now() WHERE atom_id = $1",
                    r["atom_id"], kind, p, top if top in KINDS else None, res.model, res.cost)
                counts["answered"] += 1
                kinds[kind] = kinds.get(kind, 0) + 1
                cost += res.cost or 0.0

        await asyncio.gather(*(one(r) for r in rows))
        print(json.dumps({**counts, "kinds": kinds, "cost_usd": round(cost, 4),
                          "kind_threshold": settings.kind_threshold}))
    finally:
        await conn.close()


asyncio.run(main())
