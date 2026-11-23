-- Dry run of the TIER 2 proposal (wrong year also appears in the atom text).
-- Read-only (rolls back). Counts only; no text leaves the database.
--   psql "$DB" -f scripts/phase1/remembered_on_tier2_dryrun.sql
--
-- Proposal:
--   A  the wrong year appears exactly once in the text: replace it with the
--      corrected year, fix remembered_on, save the old text and date to an undo
--      table, re-embed the new text.
--   B  otherwise (it appears more than once): set verification.date_suspect and
--      change nothing else.
-- Extra column for A: whether that one occurrence sits next to the same day and
-- month (ISO "2024-12-01", or "1 Dec 2024" / "Dec 1, 2024" / "December 1st 2024"),
-- i.e. is very likely the date itself rather than some other mention of the year.
BEGIN;
\ir remembered_on_candidates.sql
CREATE TEMP TABLE t2 ON COMMIT DROP AS
SELECT c.*, a.text_content,
       extract(year FROM c.old_remembered_on)::int AS wy,
       regexp_count(a.text_content, '\m' || extract(year FROM c.old_remembered_on)::int || '\M') AS occurrences,
       to_char(c.old_remembered_on, 'FMDD') AS d, to_char(c.old_remembered_on, 'Mon') AS mon3
FROM cand c JOIN atoms a ON a.id = c.atom_id
WHERE c.tier = 2;

SELECT agent_name AS agent,
       count(*) AS tier2,
       count(*) FILTER (WHERE occurrences = 1) AS a_replace_once,
       count(*) FILTER (WHERE occurrences = 1 AND (
           iso_in_text
           OR text_content ~* ('\m' || d || '(st|nd|rd|th)?\s+' || mon3 || '[a-z]*\.?,?\s+' || wy || '\M')
           OR text_content ~* ('\m' || mon3 || '[a-z]*\.?\s+' || d || '(st|nd|rd|th)?,?\s+' || wy || '\M')
       )) AS a_of_which_next_to_same_date,
       count(*) FILTER (WHERE occurrences = 1 AND same_year) AS a_same_year,
       count(*) FILTER (WHERE occurrences = 1 AND NOT same_year) AS a_following_year,
       count(*) FILTER (WHERE occurrences <> 1) AS b_flag_date_suspect,
       count(*) FILTER (WHERE occurrences > 2) AS b_of_which_3plus
FROM t2
GROUP BY ROLLUP(agent_name)
ORDER BY agent_name NULLS LAST;

-- Atoms in A that already have lifecycle edges (re-embedding does not touch edges).
SELECT 'A atoms with lifecycle edges' AS q, count(DISTINCT t2.atom_id)
FROM t2 JOIN edges e ON (e.source_id = t2.atom_id OR e.target_id = t2.atom_id)
WHERE t2.occurrences = 1 AND e.edge_type IN ('supersedes', 'narrows', 'tension_with');
ROLLBACK;
