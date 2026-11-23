"""Time /remember end to end on synthetic notes (v2 Phase 1, gate G2).

Creates a scratch operator + agent on the target server (admin key), stores
each note in scripts/eval/data/synthetic_notes.json, and measures wall time
until the atoms are stored: for a queued response (pre-Phase-1 server), it
polls /stores/{id}/status until complete. Prints a JSON summary. Synthetic
text only; point it at a test server, never production.

  python scripts/eval/remember_latency.py --base-url http://127.0.0.1:8079 \
      --admin-key ... --label phase1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from pathlib import Path

import httpx

DATA = Path(__file__).resolve().parent / "data" / "synthetic_notes.json"


def q(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


async def main(args) -> None:
    notes = json.loads(DATA.read_text())[: args.n]
    admin = {"X-Admin-Key": args.admin_key}
    tag = uuid.uuid4().hex[:6]
    async with httpx.AsyncClient(base_url=args.base_url, timeout=180.0) as h:
        op = (await h.post("/v1/admin/operators", headers=admin, json={
            "username": f"lat{tag}", "org": "latency", "display_name": f"latency {tag}", "email": f"lat{tag}@test.invalid",
        })).json()
        agent = (await h.post("/v1/agents", headers={"X-Operator-Key": op["api_key"]},
                              json={"name": f"lat-{tag}"})).json()
        hdr = {"X-Agent-Key": agent["agent_key"]}
        times, statuses = [], []
        for n in notes:
            body = {"text": n["text"], "remembered_on": n["date"] + "T12:00:00Z"}
            t0 = time.monotonic()
            r = await h.post(f"/v1/agents/{agent['id']}/remember", headers=hdr, json=body)
            data = r.json()
            status = data.get("status")
            if status == "queued":
                while True:
                    s = (await h.get(f"/v1/stores/{data['store_id']}/status", headers=hdr)).json()
                    if s["status"] in ("complete", "failed"):
                        status = s["status"]
                        break
                    await asyncio.sleep(0.05)
            times.append(time.monotonic() - t0)
            statuses.append(status)
        await h.post(f"/v1/admin/agents/{agent['id']}/depart", headers=admin)
    print(json.dumps({
        "label": args.label, "n": len(times), "statuses": {s: statuses.count(s) for s in set(statuses)},
        "mean_s": round(statistics.mean(times), 3), "p50_s": round(q(times, .5), 3),
        "p95_s": round(q(times, .95), 3), "max_s": round(max(times), 3),
    }))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--admin-key", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--n", type=int, default=24)
    asyncio.run(main(ap.parse_args()))
