-- Tom's 15 judged Phase 1 supersedes pairs: the legacy edge next to v2's decision.
-- FOR TOM TO RUN HIMSELF. It prints atom text; its output must not go into the
-- repo, logs, commits, reports or any provider.
--
--   psql "postgresql://mnemo_migrator@localhost:5432/mnemo" -X -f scripts/phase2/review_phase1_pairs.sql
--
-- Needs the backfill rows from scripts/phase2/redecide_pairs.py --since 2026-11-23T21:56:57Z.
-- The two pairs Tom confirmed wrong are marked; neither may come out as `replaces`.
\set ON_ERROR_STOP on
\pset expanded on
\pset format wrapped
BEGIN READ ONLY;
SET LOCAL timezone = 'UTC';
SELECT row_number() OVER (ORDER BY e.created_at, e.id) AS n,
       e.id AS legacy_edge_id,
       CASE WHEN e.id = 'b5cec0b3-010b-4a35-bed8-ce4f8bf3595a'
              OR (e.source_id = 'd8589da9-cd8d-404b-9740-7706a45ff1ed' AND e.target_id = '823680ee-62aa-4488-8c08-5cfc599e84ce')
            THEN 'CONFIRMED WRONG by Tom' END AS tom,
       round(e.weight::numeric, 3) AS legacy_weight,
       e.metadata->>'reasoning' AS legacy_reasoning,
       d.final_relation AS v2_final, d.raw_choice AS v2_raw, d.guard_fired AS v2_guard,
       d.inputs->'new'->>'kind' AS new_kind, d.inputs->'old'->>'kind' AS old_kind,
       d.inputs->'new'->>'valid_from' AS new_valid_from, d.inputs->'new'->>'precision' AS new_precision,
       d.inputs->'old'->>'valid_from' AS old_valid_from, d.inputs->'old'->>'precision' AS old_precision,
       d.probabilities AS v2_probabilities,
       s.text_content AS new_text, t.text_content AS old_text
FROM edges e
JOIN atoms s ON s.id = e.source_id
JOIN atoms t ON t.id = e.target_id
LEFT JOIN relation_decisions d ON d.decided_by = 'relation_v2_backfill' AND d.inputs->>'legacy_edge_id' = e.id::text
WHERE e.edge_type = 'supersedes' AND e.created_at >= '2026-11-23 21:56:57+00' AND e.created_at < '2026-11-24 19:18+00'
ORDER BY e.created_at, e.id;
ROLLBACK;
