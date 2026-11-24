"""Backfill 8.2, step 2 of 3: ask the time_unresolved question for planned atoms.

    docker exec -i mnemo-server-mnemo-server-1 sh -c 'cd /app && .venv/bin/python -' \\
        < scripts/phase2/backfill_time_ask.py

Production decision client (provider {"zdr": true}). Fills repair_time_2026_12
(time_unresolved = p >= 0.5, p, model, cost); atoms are untouched until step 3.
Re-runnable: only unasked rows. Prints counts only.
"""

import asyncio
import json

import asyncpg

from mnemo.server.config import settings
from mnemo.server.decisions import get_decision_model
from mnemo.server.decisions.questions import TIME_UNRESOLVED, atom_state
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
            FROM repair_time_2026_12 u JOIN atoms a ON a.id = u.atom_id LEFT JOIN sources s ON s.id = a.source_id
            WHERE u.asked_at IS NULL AND u.reverted_at IS NULL ORDER BY u.atom_id
            """)
        sem, lock = asyncio.Semaphore(CONCURRENCY), asyncio.Lock()
        counts = {"to_ask": len(rows), "unresolved": 0, "resolved_or_state": 0, "errors": 0}
        cost = 0.0

        async def one(r):
            nonlocal cost
            if r["date_basis"]:
                vf, prec, basis = r["valid_from"] or r["created_at"], r["date_precision"], r["date_basis"]
            else:
                vf, prec, basis = legacy_dates(r["text_content"], r["remembered_on"], r["created_at"])
            state = atom_state(r["text_content"], r["source_quote"], r["speaker"], None, r["remembered_on"],
                               r["created_at"], vf if basis == "event" else None, prec)
            async with sem:
                res = await model.ask(state, {"time_unresolved": TIME_UNRESOLVED})
            p = res.p_true("time_unresolved") if res.ok else None
            async with lock:
                if p is None:
                    counts["errors"] += 1
                    await conn.execute("UPDATE repair_time_2026_12 SET error = $2 WHERE atom_id = $1",
                                       r["atom_id"], (res.error or "no answer")[:200])
                    return
                flag = p >= 0.5
                await conn.execute(
                    "UPDATE repair_time_2026_12 SET time_unresolved = $2, p = $3, model = $4, cost = $5, "
                    "error = NULL, asked_at = now() WHERE atom_id = $1", r["atom_id"], flag, p, res.model, res.cost)
                counts["unresolved" if flag else "resolved_or_state"] += 1
                cost += res.cost or 0.0

        await asyncio.gather(*(one(r) for r in rows))
        print(json.dumps({**counts, "cost_usd": round(cost, 4)}))
    finally:
        await conn.close()


asyncio.run(main())
