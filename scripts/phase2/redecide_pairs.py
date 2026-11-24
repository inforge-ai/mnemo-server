"""Re-decide legacy pairs with v2, in shadow (docs/phase2-design.md §6, §9 step 4).

Runs INSIDE the server container (the app's DB role, the production decision
client and key, every request with provider {"zdr": true}):

    docker exec -i mnemo-server-mnemo-server-1 sh -c 'cd /app && .venv/bin/python - \\
        --since 2026-11-23T21:56:57Z --until 2026-11-24T19:18:00Z' \\
        < scripts/phase2/redecide_pairs.py

Pairs: legacy lifecycle edges (decided_by = 'legacy_lifecycle'), selected by
--edge-types (default supersedes), --since (edge created_at) and/or --edge-ids.
For each pair (new = edge source, old = edge target) it asks the same questions
as the store path (kinds for `unknown` sides, the time question where needed,
all four pair questions) and writes one relation_decisions row with
decided_by = 'relation_v2_backfill', mode = 'shadow', inputs.legacy_edge_id.
Nothing else changes: no edges, no status, no conflicts. Idempotent: a pair
that already has a backfill row for that edge is skipped. Prints counts only.
"""

import argparse
import asyncio
import json
import sys

import asyncpg
from pgvector.asyncpg import register_vector

from mnemo.server.config import settings
from mnemo.server.decisions import get_decision_model
from mnemo.server.decisions.questions import QUESTIONS_VERSION, TIME_UNRESOLVED, atom_state, kind_question, pair_questions, pair_state
from mnemo.server.services import relation_service as rs
from mnemo.server.services.relations import decide

ROW_SQL = rs._ONE_SQL.replace("AND a.is_active AND a.status = 'active'", "")


async def classify(model, c, th):
    qs = {}
    if c.kind == "unknown":
        qs["kind"] = kind_question()
    tp = not c.time_known and rs.time_prefilter(c.text, c.valid_from if c.basis == "event" else None)
    if tp:
        qs["time_unresolved"] = TIME_UNRESOLVED
    if not qs:
        return
    st = atom_state(c.text, c.quote, c.speaker, None, c.remembered_on, c.created_at,
                    c.valid_from if c.basis == "event" else None, c.precision)
    res = await model.ask(st, qs)
    if res.ok:
        lazy = rs._class_from(res, tp, th)
        if c.kind != "unknown":
            lazy.kind, lazy.kind_p = c.kind, c.kind_p
        c.lazy = lazy


async def main(args) -> None:
    model = get_decision_model()
    if model is None:
        raise SystemExit("MNEMO_DECISION_PROVIDER is off")
    th = rs.thresholds()
    conn = await asyncpg.connect(settings.database_url)
    await register_vector(conn)
    try:
        edges = await conn.fetch(
            """
            SELECT e.id, e.source_id, e.target_id, e.edge_type, a.agent_id
            FROM edges e JOIN atoms a ON a.id = e.source_id
            WHERE e.decided_by = 'legacy_lifecycle' AND e.edge_type = ANY($1::text[])
              AND ($2::timestamptz IS NULL OR e.created_at >= $2)
              AND ($4::timestamptz IS NULL OR e.created_at < $4)
              AND ($3::uuid[] IS NULL OR e.id = ANY($3))
              AND NOT EXISTS (SELECT 1 FROM relation_decisions d
                              WHERE d.decided_by = 'relation_v2_backfill' AND d.inputs->>'legacy_edge_id' = e.id::text)
            ORDER BY e.created_at
            """,
            args.edge_types.split(","), args.since, args.edge_ids.split(",") if args.edge_ids else None, args.until)
        counts = {"pairs": len(edges), "decided": 0, "no_answer": 0, "missing_atom": 0, "cost": 0.0}
        finals = {}
        sem = asyncio.Semaphore(args.concurrency)

        async def one(e):
            async with sem:
                n = await conn_pool_fetch(e["source_id"], e["agent_id"])
                o = await conn_pool_fetch(e["target_id"], e["agent_id"])
                if n is None or o is None:
                    counts["missing_atom"] += 1
                    return None
                new, old = rs.candidate_from_row(n), rs.candidate_from_row(o)
                await asyncio.gather(classify(model, new, th), classify(model, old, th))
                ns, os_ = new.side(), old.side()
                res = await model.ask(pair_state(rs._side_dict(old.text, os_), rs._side_dict(new.text, ns)),
                                      pair_questions())
                counts["cost"] += res.cost or 0.0
                return e, new, old, ns, os_, res

        lock = asyncio.Lock()

        async def conn_pool_fetch(atom_id, agent_id):
            async with lock:
                return await conn.fetchrow(ROW_SQL, atom_id, agent_id)

        outs = await asyncio.gather(*(one(e) for e in edges))
        for out in outs:
            if out is None:
                continue
            e, new, old, ns, os_, res = out
            ans = rs._answers(res) if res.ok else None
            inputs = {"new": ns.to_json(), "old": os_.to_json(), "questions": QUESTIONS_VERSION,
                      "legacy_edge_id": str(e["id"]), "legacy_edge_type": e["edge_type"],
                      "new_kind_top": new.lazy.kind_top if new.lazy else new.kind,
                      "new_kind_p": new.lazy.kind_p if new.lazy else new.kind_p,
                      "old_kind_top": old.lazy.kind_top if old.lazy else old.kind,
                      "old_kind_p": old.lazy.kind_p if old.lazy else old.kind_p,
                      "old_kind_stored": old.kind, "new_kind_stored": new.kind,
                      "old_kind_lazy": bool(old.lazy and old.kind == "unknown")}
            if ans is None:
                final, guard, raw, probs = "pending", "no_answer", None, None
                counts["no_answer"] += 1
            else:
                d = decide(ns, os_, ans, th)
                final, guard, raw = d.final, d.guard, d.raw
                probs = {"relation": ans.relation, "change": ans.change, "same_attribute": ans.same_attribute,
                         "correction": ans.correction}
                counts["decided"] += 1
            finals[final] = finals.get(final, 0) + 1
            await conn.execute(
                """
                INSERT INTO relation_decisions (agent_id, new_atom_id, old_atom_id, source_id, model, probabilities,
                    inputs, raw_choice, final_relation, guard_fired, mode, decided_by, attempts, latency_ms, cost,
                    decided_at)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,$10,'shadow','relation_v2_backfill',$11,$12,$13,
                        CASE WHEN $9 = 'pending' THEN NULL ELSE now() END)
                """,
                e["agent_id"], e["source_id"], e["target_id"], new.source_id, res.model,
                json.dumps(probs) if probs else None, json.dumps(inputs, default=str), raw, final, guard,
                max(1, res.attempts), res.latency_ms, res.cost)
        counts["cost"] = round(counts["cost"], 6)
        print(json.dumps({**counts, "final_relation": finals}))
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge-types", default="supersedes")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--edge-ids", default="")
    ap.add_argument("--concurrency", type=int, default=8)
    a = ap.parse_args(sys.argv[1:])
    from datetime import datetime
    for name in ("since", "until"):
        if getattr(a, name):
            setattr(a, name, datetime.fromisoformat(getattr(a, name).replace("Z", "+00:00")))
    asyncio.run(main(a))
