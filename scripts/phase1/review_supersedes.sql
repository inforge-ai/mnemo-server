-- Review of supersedes edges created since the 2026-11-23 21:56 UTC restart.
-- FOR TOM TO RUN HIMSELF. It prints atom text, so its output must not go into
-- the repo, logs, commits, reports or any provider.
--
--   psql "postgresql://mnemo_migrator@localhost:5432/mnemo" -X -f scripts/phase1/review_supersedes.sql
--
-- Optional: -v since='2026-11-23 21:56+00'. Read-only (rolls back).
\set ON_ERROR_STOP on
\if :{?since}
\else
\set since '2026-11-23 21:56+00'
\endif
\pset expanded on
\pset format wrapped
BEGIN READ ONLY;
SET LOCAL timezone = 'UTC';
SELECT row_number() OVER (ORDER BY e.created_at, e.id)  AS n,
       e.id                                            AS edge_id,
       to_char(e.created_at, 'YYYY-MM-DD HH24:MI')     AS edge_created,
       (SELECT name FROM agents WHERE id = s.agent_id) AS agent,
       round(e.weight::numeric, 3)                     AS weight,
       round((e.metadata->>'cosine_at_detection')::numeric, 3) AS cosine,
       e.metadata->>'reasoning'                        AS reasoning,
       s.id                                            AS new_atom_id,
       s.atom_type                                     AS new_type,
       to_char(s.remembered_on, 'YYYY-MM-DD HH24:MI')  AS new_remembered_on,
       s.text_content                                  AS new_text,
       t.id                                            AS old_atom_id,
       t.atom_type                                     AS old_type,
       to_char(t.remembered_on, 'YYYY-MM-DD HH24:MI')  AS old_remembered_on,
       t.text_content                                  AS old_text
FROM edges e
JOIN atoms s ON s.id = e.source_id   -- new atom (supersedes ...)
JOIN atoms t ON t.id = e.target_id   -- superseded atom
WHERE e.edge_type = 'supersedes'
  AND e.created_at >= :'since'::timestamptz
ORDER BY e.created_at, e.id;
ROLLBACK;
