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

## Rollback

- **Code:** `scripts/deploy/image_tags.sh rollback 9b5776b`. Migration 011 stays; the old code works with it.
- **Data:** each backfill's undo script.
- **Relations:** set `MNEMO_RELATION_MODE=off` and recreate the container.
