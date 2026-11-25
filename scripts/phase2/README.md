# Phase 2 deploy runbook (gate G2)

Each **STOP** means report the counts and wait for Tom. Production data changes follow the same pattern every time: dry run → STOP → guarded execute (`-v expected=N`) → verify. Every change has an undo table and an undo script. Counts and ids only.

```bash
DB="postgresql://mnemo_migrator@localhost:5432/mnemo"   # ~/.pgpass
cd ~/mnemo-server
```

## 0. Before

- `scripts/deploy/check_free_space.sh || exit 1`
- Check memory: `free -m`; `docker stats --no-stream`.
- `scripts/deploy/image_tags.sh pre`. The running image is already tagged `mnemo-server:9b5776b`, which is the rollback image.
- Back up the database:
  ```bash
  pg_dump --format=custom --file ~/backups/mnemo-prod-$(date +%F)-pre-phase2.dump "$DB"
  sha256sum ~/backups/mnemo-prod-$(date +%F)-pre-phase2.dump
  ```
  Verify the dump by checking that its row counts for `atoms` and `edges` match the live counts. Keep every earlier dump.
- `.env` (back it up first): leave `MNEMO_RELATION_MODE` unset or set it to `off`; set `MNEMO_DECISION_PROVIDER=openrouter`. `MNEMO_OPENROUTER_API_KEY` is already set; check only that it is set, never read it. Leave `MNEMO_RECALL_HIDE_SUPERSEDED=false` until G3.

## 1. Deploy with relations off

```bash
UV_FROZEN=1 uv run mnemo migrate --dry-run --database-url "$DB"   # expect: Pending: 011_relations
UV_FROZEN=1 uv run mnemo migrate --database-url "$DB"             # about 0.4 s (dry run 2026-11-24)
BUILD_COMMIT=$(git rev-parse --short HEAD) docker compose build mnemo-server
docker compose up -d mnemo-server
curl -s http://127.0.0.1:8000/v1/health                           # schema 011_relations, version +<commit>
scripts/deploy/image_tags.sh post
```

Migration 011 goes first, because the app role cannot apply it at startup. The old container keeps working on the new schema: the new columns have defaults, and the trigger keeps `status` in step with `is_active`.

**From this restart on, every write uses:**
- decomposer prompt v3 (`event_date`, `date_precision`, no confidence), and
- the confidence prior by `origin_class`,

whatever the relation mode is.

While the mode is `off`, new atoms keep kind `unknown` and get no model grounding answer. They are picked up by backfill 8.3, or classified on demand once shadow is on.

**Smoke test** with a scratch agent: remember → 201 with `kind` / `valid_from` / `date_precision` in the response; `decomposer_version` ends in `prompt_v3`; no `relation_decisions` rows. Then purge the agent and delete it in a guarded transaction.

## 2. Backfill 8.1: dates (deterministic)

```bash
psql "$DB" -X -f scripts/phase2/backfill_dates.sql                  # dry run → STOP
psql "$DB" -X -v expected=N -f scripts/phase2/backfill_dates.sql    # execute
# undo: psql "$DB" -X -v expected=N -f scripts/phase2/backfill_dates_undo.sql
```

## 3. Backfill 8.2: time unresolved (decision model)

```bash
psql "$DB" -X -f scripts/phase2/backfill_time_plan.sql                  # dry run → STOP
psql "$DB" -X -v expected=N -f scripts/phase2/backfill_time_plan.sql    # record the plan
docker exec -i mnemo-server-mnemo-server-1 sh -c 'cd /app && .venv/bin/python -' < scripts/phase2/backfill_time_ask.py
psql "$DB" -X -v expected=N -f scripts/phase2/backfill_time_apply.sql   # → STOP before apply if the answers look off
# undo: backfill_time_undo.sql
```

## 4. Backfill 8.3: kind (decision model)

```bash
psql "$DB" -X -f scripts/phase2/backfill_kind_plan.sql                  # dry run (count, cost) → STOP
psql "$DB" -X -v expected=N -f scripts/phase2/backfill_kind_plan.sql
docker exec -i mnemo-server-mnemo-server-1 sh -c 'cd /app && .venv/bin/python -' < scripts/phase2/backfill_kind_ask.py
psql "$DB" -X -v expected=N -f scripts/phase2/backfill_kind_apply.sql
# undo: backfill_kind_undo.sql
```

## 5. Shadow on

- In `.env`, set `MNEMO_RELATION_MODE=shadow`.
- Run `docker compose up -d mnemo-server`. Compose reads `env_file` only when it creates a container, so a recreate is needed.
- Check that the container has the setting: run `printenv | grep -c MNEMO_RELATION_MODE=shadow` inside it.
- The first real store writes `relation_decisions` rows with `mode = 'shadow'`, and no `relation_v2` edges.

## 6. After shadow is on: Tom's 15 Phase 1 pairs

```bash
docker exec -i mnemo-server-mnemo-server-1 sh -c 'cd /app && .venv/bin/python - \
    --since 2026-11-23T21:56:57Z --until 2026-11-24T19:18:00Z' < scripts/phase2/redecide_pairs.py
# prints counts only: {"pairs": 15, "decided": ..., "final_relation": {...}}
```

Then Tom runs `psql "$DB" -X -f scripts/phase2/review_phase1_pairs.sql` himself. It prints text, so the output stays local. Requirement: the two pairs he confirmed wrong must not come out as `replaces`.

## 7. During shadow (at least 7 days or 100 decided pairs)

- Tom: `scripts/phase2/review_shadow.sql -v out=<path outside the repo>`.
- Me: the counts-only shadow report, then **STOP** at G3.

## 8. Deploy 2 (Tom, 2026-11-25): legacy tagging, migration 012, 8.3b

Shadow stays on; one restart. Not between 23:45 and 00:10 UTC; no store in flight.

```bash
UV_FROZEN=1 uv run mnemo migrate --dry-run --database-url "$DB"   # expect: Pending: 012_kind_probs
UV_FROZEN=1 uv run mnemo migrate --database-url "$DB"
scripts/deploy/image_tags.sh pre
BUILD_COMMIT=$(git rev-parse --short HEAD) docker compose build mnemo-server
docker compose up -d mnemo-server
curl -s http://127.0.0.1:8000/v1/health                           # schema 012_kind_probs
scripts/deploy/image_tags.sh post
```

From this restart on: new lifecycle edges carry `decided_by = 'legacy_lifecycle'`; new atoms and lazily classified candidates store `kind_probs`, and `relation_decisions.inputs` carries both sides' distributions. Production stays `relation_matrix=as_built`, `relation_kind_rule=argmax`.

**Backfill 8.3b** asks the kind question again for active atoms with no `kind_probs`. It writes `kind_probs`, and `kind` / `kind_p` as its argmax.

```bash
psql "$DB" -X -f scripts/phase2/backfill_kind_probs_plan.sql                  # dry run: count, cost
psql "$DB" -X -v expected=N -f scripts/phase2/backfill_kind_probs_plan.sql    # if cost < $0.50 (Tom)
docker exec -i mnemo-server-mnemo-server-1 sh -c 'cd /app && .venv/bin/python -' < scripts/phase2/backfill_kind_probs_ask.py
psql "$DB" -X -v expected=N -f scripts/phase2/backfill_kind_probs_apply.sql
# undo: backfill_kind_probs_undo.sql
```

## 9. G3 replay

```bash
docker exec -i mnemo-server-mnemo-server-1 sh -c 'cd /app && .venv/bin/python - --since 2026-11-25T12:03:00Z' \
    < scripts/phase2/g3_replay.py
```

The replay covers three variants (as_built, symmetric, symmetric + marginal), each at kind thresholds 0.5 / 0.6 / 0.7. It prints counts only.

## Rollback

- **Code:** `scripts/deploy/image_tags.sh rollback <previous tag>` (before deploy 2: `9f72c42`; before G2: `9b5776b`). Migrations 011 and 012 stay; the old code works with both.
- **Data:** each backfill's undo script.
- **Relations:** set `MNEMO_RELATION_MODE=off` and recreate the container.
