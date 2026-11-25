"""G3: replay every stored shadow decision under each decision variant (Tom, 2026-11-25).

    docker exec -i mnemo-server-mnemo-server-1 sh -c 'cd /app && .venv/bin/python - [--since ISO]' \\
        < scripts/phase2/g3_replay.py

Variants: as_built, symmetric (state<->rule), symmetric + marginal; each at kind
thresholds 0.5 / 0.6 / 0.7. No model calls: relations.replay() re-runs decide()
on each row's stored inputs and probabilities. Rows written before migration
012 carry no kind distributions; those are taken from atoms.kind_probs (filled
by backfill 8.3b) and counted as `probs_from_atoms`.

Prints counts only (no text, no ids): final relation per variant x threshold,
how many rows each variant changes against the recorded decision, and
transitions (recorded -> replayed). Rows: mode shadow, decided_by relation_v2,
final_relation not pending.
"""

import asyncio
import json
import sys
from collections import Counter
from datetime import datetime

import asyncpg

from mnemo.server.config import settings
from mnemo.server.services.relations import Thresholds, replay

VARIANTS = [("as_built", {}), ("symmetric", {"matrix": "state_rule_symmetric"}),
            ("symmetric+marginal", {"matrix": "state_rule_symmetric", "kind_rule": "marginal"})]
KIND_THRESHOLDS = (0.5, 0.6, 0.7)


def _j(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


async def main() -> None:
    since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else "2026-11-25T12:03:00+00:00"
    since_ts = datetime.fromisoformat(since.replace("Z", "+00:00"))
    conn = await asyncpg.connect(settings.database_url)
    try:
        rows = await conn.fetch(
            """
            SELECT d.inputs, d.probabilities, d.final_relation, n.kind_probs AS new_probs, o.kind_probs AS old_probs
            FROM relation_decisions d
            LEFT JOIN atoms n ON n.id = d.new_atom_id
            JOIN atoms o ON o.id = d.old_atom_id
            WHERE d.mode = 'shadow' AND d.decided_by = 'relation_v2' AND d.final_relation <> 'pending'
              AND d.probabilities IS NOT NULL AND d.created_at >= $1::timestamptz
            """, since_ts)
    finally:
        await conn.close()

    base = Thresholds(
        same=settings.relation_threshold_same, replaces=settings.relation_threshold_replaces,
        conflict=settings.relation_threshold_conflict, refines=settings.relation_threshold_refines,
        change=settings.relation_threshold_change, same_attribute=settings.relation_threshold_same_attribute,
        correction=settings.relation_threshold_correction)
    out = {"rows": len(rows), "since": since, "recorded": dict(Counter(r["final_relation"] for r in rows)),
           "probs_from_atoms": 0, "no_probs": 0, "variants": {}}
    for r in rows:
        inputs = _j(r["inputs"])
        if not (inputs.get("new", {}).get("kind_probs") and inputs.get("old", {}).get("kind_probs")):
            if r["old_probs"] is None or (r["new_probs"] is None and not inputs.get("new", {}).get("kind_probs")):
                out["no_probs"] += 1
            else:
                out["probs_from_atoms"] += 1
    for name, kw in VARIANTS:
        for k in KIND_THRESHOLDS:
            finals, changed, trans = Counter(), 0, Counter()
            th = Thresholds(**{**base.__dict__, "kind": k, **kw})
            for r in rows:
                kp = {"new": _j(r["new_probs"]) or None, "old": _j(r["old_probs"]) or None}
                f = replay(_j(r["inputs"]), _j(r["probabilities"]), th, kind_probs=kp).final
                finals[f] += 1
                if f != r["final_relation"]:
                    changed += 1
                    trans[f"{r['final_relation']}->{f}"] += 1
            out["variants"][f"{name}@{k}"] = {"final": dict(finals), "changed_vs_recorded": changed,
                                             "transitions": dict(trans.most_common())}
    print(json.dumps(out, indent=1))


asyncio.run(main())
