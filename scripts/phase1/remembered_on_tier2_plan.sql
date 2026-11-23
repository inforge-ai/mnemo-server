-- Tier 2 (the wrong year also appears in the atom text), step 1 of 3: PLAN.
-- Writes the plan to the undo table repair_date_text_2026_11; atoms are NOT changed.
--
--   dry run (rolls back):  psql "$DB" -v agents=clio,tapio,nels-claude-desktop -f remembered_on_tier2_plan.sql
--   record the plan:       ... -v expected_replace=N -v expected_flag=M -f remembered_on_tier2_plan.sql
--
-- Sets, within tier 2 and the given agents:
--   replace: the wrong year appears exactly once, sits next to the same day and
--            month (ISO "2024-12-01", "1 Dec 2024", "Dec 1, 2024", "December 1st 2024"),
--            AND the corrected date is in created_at's own year.
--            → new text = that one year replaced by the corrected year;
--              new remembered_on = the corrected date; re-embedded in step 2.
--   wait:    the corrected date is in the following year. Not planned (held with
--            the following-year tier-1 decision).
--   flag:    everything else (same-year corrections only): verification.flags gets
--            "date_suspect"; text, date and embedding unchanged.
-- Steps 2 (remembered_on_tier2_embed.py) and 3 (remembered_on_tier2_apply.sql) follow.
-- Counts and ids only; no text leaves the database.
\set ON_ERROR_STOP on
\if :{?agents}
\else
  \echo 'usage: -v agents=a,b [-v expected_replace=N -v expected_flag=M]'
  \quit
\endif
BEGIN;
\ir remembered_on_candidates.sql
CREATE TEMP TABLE t2 ON COMMIT DROP AS
SELECT c.*, a.text_content, a.embedding, a.verification,
       extract(year FROM c.old_remembered_on)::int AS wy,
       extract(year FROM c.new_remembered_on)::int AS ny,
       regexp_count(a.text_content, '\m' || extract(year FROM c.old_remembered_on)::int || '\M') AS occurrences,
       c.iso_in_text
         OR a.text_content ~* ('\m' || to_char(c.old_remembered_on, 'FMDD') || '(st|nd|rd|th)?\s+'
                               || to_char(c.old_remembered_on, 'Mon') || '[a-z]*\.?,?\s+'
                               || extract(year FROM c.old_remembered_on)::int || '\M')
         OR a.text_content ~* ('\m' || to_char(c.old_remembered_on, 'Mon') || '[a-z]*\.?\s+'
                               || to_char(c.old_remembered_on, 'FMDD') || '(st|nd|rd|th)?,?\s+'
                               || extract(year FROM c.old_remembered_on)::int || '\M') AS next_to_date
FROM cand c JOIN atoms a ON a.id = c.atom_id
WHERE c.tier = 2 AND c.agent_name = ANY(string_to_array(:'agents', ','));

-- 1. The table Tom approved against (2026-11-23): tier2 217; once 195; once and
--    next to the date 104; once same/following year 164/31; more than once 22.
SELECT agent_name AS agent, count(*) AS tier2,
       count(*) FILTER (WHERE occurrences = 1) AS once,
       count(*) FILTER (WHERE occurrences = 1 AND next_to_date) AS once_next_to_date,
       count(*) FILTER (WHERE occurrences = 1 AND same_year) AS once_same_year,
       count(*) FILTER (WHERE occurrences = 1 AND NOT same_year) AS once_following_year,
       count(*) FILTER (WHERE occurrences <> 1) AS more_than_once
FROM t2 GROUP BY ROLLUP(agent_name) ORDER BY agent_name NULLS LAST;

-- 2. This plan.
CREATE TEMP TABLE plan ON COMMIT DROP AS
SELECT atom_id, agent_id, agent_name,
       CASE WHEN occurrences = 1 AND next_to_date AND same_year THEN 'replace'
            WHEN NOT same_year THEN 'wait'
            ELSE 'flag' END AS action,
       old_remembered_on, new_remembered_on, text_content AS old_text,
       regexp_replace(text_content, '\m' || wy || '\M', ny::text) AS new_text,
       embedding AS old_embedding, verification AS old_verification
FROM t2;
SELECT agent_name AS agent, action, count(*) AS atoms FROM plan
GROUP BY ROLLUP(agent_name, action) HAVING (agent_name IS NULL) = (action IS NULL) OR agent_name IS NOT NULL
ORDER BY agent_name NULLS LAST, action;
SELECT action, count(*) AS atoms FROM plan GROUP BY ROLLUP(action) ORDER BY action NULLS LAST;
-- Sanity: every replace changes exactly one year and leaves no wrong year behind.
SELECT 'replace sanity' AS q, count(*) AS rows,
       count(*) FILTER (WHERE new_text = old_text) AS unchanged_text,
       count(*) FILTER (WHERE length(new_text) <> length(old_text)) AS length_changed,
       count(*) FILTER (WHERE new_text ~ ('\m' || extract(year FROM old_remembered_on)::int || '\M')) AS wrong_year_left
FROM plan WHERE action = 'replace';

\if :{?expected_replace}
\if :{?expected_flag}
SELECT (SELECT count(*) FROM plan WHERE action = 'replace') = :expected_replace
   AND (SELECT count(*) FROM plan WHERE action = 'flag') = :expected_flag
   AND NOT EXISTS (SELECT 1 FROM plan WHERE action = 'replace' AND (new_text = old_text OR length(new_text) <> length(old_text))) AS ok \gset
\if :ok
INSERT INTO repair_date_text_2026_11 (atom_id, agent_id, action, old_remembered_on, new_remembered_on,
                                      old_text, new_text, old_embedding, old_verification)
SELECT atom_id, agent_id, action, old_remembered_on,
       CASE WHEN action = 'replace' THEN new_remembered_on END,
       CASE WHEN action = 'replace' THEN old_text END,
       CASE WHEN action = 'replace' THEN new_text END,
       CASE WHEN action = 'replace' THEN old_embedding END,
       old_verification
FROM plan WHERE action IN ('replace', 'flag');
-- Step 2 runs as the app role inside the container: it reads new_text and writes new_embedding.
GRANT SELECT, UPDATE (new_embedding) ON repair_date_text_2026_11 TO mnemo;
COMMIT;
\echo 'planned' :expected_replace 'replace,' :expected_flag 'flag'
\else
\echo 'ABORT: plan counts differ from expected (or a replace is not a one-year swap)'
ROLLBACK;
\endif
\else
ROLLBACK;
\endif
\else
\echo 'dry run only (pass -v expected_replace=N -v expected_flag=M to record the plan)'
ROLLBACK;
\endif
