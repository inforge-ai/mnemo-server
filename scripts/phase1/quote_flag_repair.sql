-- Re-grade atoms flagged ungrounded_quote under the v2 (word-sequence) grounding check.
-- For each listed atom: restore the confidence band the downgrade removed (one band
-- up the ladder) and replace the flag:
--   -v exact_ids=...    v2 says exact   → ungrounded_quote removed, no flag added
--   -v inexact_ids=...  v2 says inexact → ungrounded_quote replaced by quote_inexact
-- (comma-separated atom ids; either may be empty ''). Atoms v2 still calls
-- ungrounded are simply not listed. The v2 verdicts come from running
-- mnemo/server/services/grounding.py quote_match() in the container.
--
--   dry run (rolls back):  psql "$DB" -X -v exact_ids=a,b -v inexact_ids=c,d -f quote_flag_repair.sql
--   execute:               ... -v expected=N -f quote_flag_repair.sql
--   undo:                  quote_flag_repair_undo.sql
-- Refuses an atom unless it is active, carries ungrounded_quote with
-- band_downgraded=true, and sits on the ladder below the top band.
-- Counts and ids only.
\set ON_ERROR_STOP on
\if :{?exact_ids}
\else
\set exact_ids ''
\endif
\if :{?inexact_ids}
\else
\set inexact_ids ''
\endif
BEGIN;
CREATE TABLE IF NOT EXISTS repair_quote_flag_2026_11 (
  atom_id           uuid PRIMARY KEY REFERENCES atoms(id) ON DELETE CASCADE,
  verdict           text NOT NULL CHECK (verdict IN ('exact', 'inexact')),
  old_alpha         double precision NOT NULL,
  old_beta          double precision NOT NULL,
  old_verification  jsonb NOT NULL,
  repaired_at       timestamptz NOT NULL DEFAULT now(),
  reverted_at       timestamptz
);
CREATE TEMP TABLE ladder (i int, alpha double precision, beta double precision) ON COMMIT DROP;
INSERT INTO ladder VALUES (0, 8, 1), (1, 4, 2), (2, 3, 2), (3, 2, 3), (4, 2, 4);  -- decomposers/base.py BETA_LADDER

CREATE TEMP TABLE req ON COMMIT DROP AS
SELECT unnest(string_to_array(nullif(:'exact_ids', ''), ','))::uuid AS atom_id, 'exact'::text AS verdict
UNION ALL
SELECT unnest(string_to_array(nullif(:'inexact_ids', ''), ','))::uuid, 'inexact';

CREATE TEMP TABLE plan ON COMMIT DROP AS
SELECT r.atom_id, r.verdict, (SELECT name FROM agents WHERE id = a.agent_id) AS agent,
       a.confidence_alpha AS old_alpha, a.confidence_beta AS old_beta, a.verification AS old_verification,
       l.i AS old_band, up.alpha AS new_alpha, up.beta AS new_beta,
       (a.id IS NOT NULL AND a.is_active
        AND a.verification->'flags' ? 'ungrounded_quote'
        AND (a.verification->>'band_downgraded')::boolean IS TRUE
        AND l.i >= 1
        AND NOT EXISTS (SELECT 1 FROM repair_quote_flag_2026_11 u WHERE u.atom_id = r.atom_id AND u.reverted_at IS NULL)) AS eligible
FROM req r
LEFT JOIN atoms a ON a.id = r.atom_id
LEFT JOIN ladder l ON l.alpha = a.confidence_alpha AND l.beta = a.confidence_beta
LEFT JOIN ladder up ON up.i = l.i - 1;

SELECT atom_id, agent, verdict, old_band, old_alpha || '/' || old_beta AS old_ab,
       new_alpha || '/' || new_beta AS new_ab, eligible,
       CASE verdict WHEN 'exact' THEN 'remove ungrounded_quote' ELSE 'ungrounded_quote -> quote_inexact' END AS flag_change
FROM plan ORDER BY verdict, atom_id;
SELECT verdict, count(*) AS atoms, count(*) FILTER (WHERE eligible) AS eligible,
       count(*) FILTER (WHERE old_band IS NOT NULL) AS on_ladder
FROM plan GROUP BY ROLLUP(verdict) ORDER BY verdict NULLS LAST;
-- Context: all atoms with ungrounded_quote since the 2026-11-23 21:56:57 restart, and how many are listed.
SELECT count(*) AS flagged_since_restart,
       count(*) FILTER (WHERE a.id IN (SELECT atom_id FROM req)) AS listed
FROM atoms a WHERE a.created_at >= '2026-11-23 21:56:57+00' AND a.verification->'flags' ? 'ungrounded_quote';

\if :{?expected}
SELECT (SELECT count(*) FROM plan) = :expected AND (SELECT bool_and(eligible) FROM plan) AS ok \gset
\if :ok
INSERT INTO repair_quote_flag_2026_11 (atom_id, verdict, old_alpha, old_beta, old_verification)
SELECT atom_id, verdict, old_alpha, old_beta, old_verification FROM plan;
UPDATE atoms a SET
  confidence_alpha = p.new_alpha,
  confidence_beta  = p.new_beta,
  verification = p.old_verification
      || jsonb_build_object(
           'checks', 'v2',
           'flags', (SELECT coalesce(jsonb_agg(f), '[]'::jsonb) FROM (
                       SELECT CASE WHEN f = 'ungrounded_quote' THEN 'quote_inexact' ELSE f END AS f
                       FROM jsonb_array_elements_text(p.old_verification->'flags') f
                       WHERE NOT (f = 'ungrounded_quote' AND p.verdict = 'exact')) x),
           'quote_found', true,
           'band_downgraded', false,
           'regraded', 'quote_flag_repair_2026_11')
FROM plan p WHERE a.id = p.atom_id;
SELECT verdict, count(*) AS repaired FROM repair_quote_flag_2026_11 WHERE reverted_at IS NULL GROUP BY verdict;
COMMIT;
\echo 'repaired' :expected
\else
\echo 'ABORT: count differs from expected, or an atom is not eligible'
ROLLBACK;
\endif
\else
\echo 'dry run only (pass -v expected=N to execute)'
ROLLBACK;
\endif
