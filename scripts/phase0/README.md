# Phase 0 data window (spec item 5)

Run with the migration role (`MNEMO_MIGRATION_DATABASE_URL`), server stopped.
Every `*_exec.sql` runs in one transaction and **rolls back unless the
affected row count equals `-v expected=N`**, the number approved at the gate.
Outputs are counts and ids only.

| step | dry run | execute |
|---|---|---|
| 5a | `mnemo migrate --dry-run` | `mnemo migrate --to 008_merge_pairs` |
| 5b | — | `5b_classify_exec.sql` (no expected: classification only) |
| 5c | `5c_restore_dryrun.sql` | `5c_restore_exec.sql -v expected=N` |
| 5c-bis | `5c_bis_dryrun.sql` | `5c_bis_exec.sql -v expected=N` |
| 5c-ter | `5c_ter_dryrun.sql` | `5c_ter_exec.sql -v expected=N` |
| 5d | `5d_eval_dryrun.sql -v extra="''"` | `5d_eval_exec.sql -v extra=... -v expected_agents=N -v expected_atoms=M` |
| 5d (mnemo_eval) | — | `5d_eval_db_keep_only_eval.sql -v extra=... -v expected_agents=N` |
| 5e | `mnemo migrate --dry-run` | `mnemo migrate`, then `5e_vacuum.sql`, `5e_latency.sql` |

`extra` is a quoted, comma-separated list of additional eval agent names
beyond `locomo-conv-%`, e.g. `-v extra="'agent1'"`; use `"''"` for none.
Run each with `psql "$URL" -X -v ON_ERROR_STOP=1 -f <file> [vars]`.
