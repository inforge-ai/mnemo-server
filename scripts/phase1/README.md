# Phase 1 deploy runbook (gate G2)

**Approved** by Tom on 2026-11-22, conditional on the Phase 0 24-hour consolidation check passing. Decomposer model: `claude-haiku-5-5`.

Same pattern as Phase 0: dry run → **STOP** if anything differs → guarded execute → verify. Counts and ids only in anything that gets recorded.

Every psql command connects as `mnemo_migrator` (`~/.pgpass`):

```bash
DB=postgresql://mnemo_migrator@localhost:5432/mnemo
```

## 0. Preconditions (read-only)

1. It is after **2026-11-23 14:31 UTC**. Every consolidation row since the restart is all zeros:

   ```sql
   SELECT count(*) AS runs,
          count(*) FILTER (WHERE (metadata->>'decayed')::int + (metadata->>'clustered')::int
                                 + (metadata->>'merged')::int + (metadata->>'pruned')::int
                                 + (metadata->>'purged')::int > 0) AS nonzero_runs,
          min(created_at), max(created_at)
   FROM access_log WHERE action = 'consolidation' AND created_at >= '2026-11-22 14:31+00';
   ```

   Expect `runs` ≥ 24 and `nonzero_runs` = 0. Also check the container logs for `Consolidation run failed` since the restart. Then tick the box on PR #23 and add the result to `docs/phase0-report-2026-11.md`.

2. `mnemo migrate --dry-run --database-url $DB` reports `Pending: 010_sources` only.

3. Tom has sent the `user_name` CSV: `agent_name,user_name`, no header. It lives outside the repo, mode 600, e.g. `~/backups/user_names.csv`.

4. `git status` is clean apart from Tom's local `docker-compose.yml` and `uv.lock`. The branch is `v2/phase1` at the reviewed commit.

## 1. Backup

```bash
pg_dump -Fc "$DB" -f ~/backups/mnemo-prod-2026-11-23.dump && chmod 600 ~/backups/mnemo-prod-2026-11-23.dump
pg_restore --list ~/backups/mnemo-prod-2026-11-23.dump | wc -l
sha256sum ~/backups/mnemo-prod-2026-11-23.dump
```

Record the path, size and sha256.

## 2. Stop, migrate

```bash
docker compose stop mnemo-server
mnemo migrate --dry-run --database-url "$DB"        # again, with the server stopped
mnemo migrate --database-url "$DB"                  # applies 010_sources
```

Verify:
- `schema_migrations` has `010_sources`.
- `\d sources` and `\d source_merges` exist.
- `atoms` has `source_id`, `source_quote` and `verification`.
- Grants: `has_table_privilege('mnemo','sources','INSERT')` is true.
- The atom count is unchanged (12,898 at the time of writing).

## 3. `user_name`

```bash
psql "$DB" -v csv=$HOME/backups/user_names.csv -f scripts/phase1/user_name_dryrun.sql
```

**STOP** and show Tom the per-agent table. Each name should have `active_matches` = 1. Agent names are unique only per operator, so a name can match agents of more than one operator.

Then, with `N` = `would_update`:

```bash
psql "$DB" -v csv=$HOME/backups/user_names.csv -v expected=N -f scripts/phase1/user_name_exec.sql
```

Verify:

```sql
SELECT count(*) FROM agents WHERE metadata ? 'user_name';
```

## 4. Configuration

Append to `.env`. Never add `MNEMO_MIGRATION_DATABASE_URL` or any OpenRouter key.

```
MNEMO_DECOMPOSER_MODEL=claude-haiku-5-5
```

Defaults already in effect, so nothing else to set:
- `MNEMO_DECOMPOSER_PROVIDER=anthropic`; the server refuses to start without `ANTHROPIC_API_KEY`.
- The real embedder.
- Every consolidation flag off.
- `MNEMO_DUPLICATE_SIMILARITY_THRESHOLD=0.97` (already in `.env`).
- `MNEMO_LIFECYCLE_DETECTION_ENABLED=true` (already in `.env`).

## 5. Rebuild, restart, check

**Free space first.** The image is about 24 GB, and a rebuild needs roughly twice that while it runs. `scripts/deploy/check_free_space.sh` aborts unless the Docker filesystem has max(30 GiB, 2 × current image + 10 GiB) free. The 10 GiB keeps Postgres and Zulip, which share the disk, writable at the build's peak. On 2026-11-23 a build started with 17 GB free and filled the disk, and Zulip's RabbitMQ was down for about 8 minutes. If it aborts, prune unused build cache and images (`docker builder prune`, `docker image prune`) after checking what they hold, and never prune volumes without looking.

```bash
scripts/deploy/check_free_space.sh || exit 1   # aborts below max(30 GiB, 2 x image + 10 GiB)
BUILD_COMMIT=$(git rev-parse --short HEAD) docker compose build mnemo-server
docker compose up -d mnemo-server
curl -s http://127.0.0.1:8000/v1/health
```

Expect `schema_version` `010_sources` and `version` ending `+<commit>`. The logs should show `Decomposer: anthropic claude-haiku-5-5` and no `marked N stuck store job(s)` beyond what is expected (0).

## 6. Smoke test (synthetic, scratch agent)

1. Create a scratch operator and agent with the admin key.
2. `remember` one synthetic note (e.g. "The Gannet export runs at 03:00 UTC."). Expect `201`, `status: complete`, ≥ 1 atom with a `source_quote`.
3. `recall` it immediately. Expect the atom id to come back with `source_id` and `origin_class`.
4. `GET /v1/agents/me` returns the scratch agent.
5. Clean up: `POST /v1/admin/agents/{id}/purge` with `{"confirm": "purge"}`. This deletes its atoms and sources. Then delete the scratch operator.

Record: health output, counts only, latency.

## 7. After G2

- Recall works for each active agent (result counts and latency only, as in Phase 0).
- Update `docs/phase1-report.md` with the deploy record.
- G3: publish **`mnemo-ai` 0.6.0rc1** (pre-release) from mnemo-client `v2/phase1`. Roll out to Lloyd first with an explicit `==0.6.0rc1` pin, then Tapio, then Clio. 0.6.0 final only on Tom's say.
- Remove the backup only if Tom says so; Phase 0's backup is also kept.
