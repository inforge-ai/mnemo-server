-- Shared by remembered_on_dryrun.sql / remembered_on_exec.sql. Builds temp table
-- `cand`: active atoms whose remembered_on looks like a wrong-year resolution.
--   rule: remembered_on >= 300 days before created_at; putting the month-day into
--         created_at's year (or the next year) lands within 90 days of created_at;
--         the shift is 1-3 years (e.g. "21 Jan" written in Nov 2026 stored as 2024-01-21 → 2027-01-21).
--   tier 1: the wrong year does not appear in the atom text (date-only fix is consistent).
--   tier 2: the wrong year appears in the text (ISO date or other form): text would
--           disagree after a date-only fix. Not repaired by these scripts.
-- Reads atoms; writes only the temp table. Counts and ids only; no text leaves the DB.
SET timezone = 'UTC';
CREATE TEMP TABLE cand ON COMMIT DROP AS
WITH b AS (
  SELECT a.id, a.agent_id, a.remembered_on, a.created_at::date AS c, a.remembered_on::date AS r,
         strpos(a.text_content, to_char(a.remembered_on::date, 'YYYY-MM-DD')) > 0 AS iso_in_text,
         a.text_content ~ ('\m' || extract(year FROM a.remembered_on)::int || '\M') AS year_in_text
  FROM atoms a
  WHERE a.is_active AND a.remembered_on <= a.created_at - interval '300 days'
), f AS (
  SELECT b.*,
    make_date(extract(year FROM c)::int,     extract(month FROM r)::int,
              CASE WHEN extract(month FROM r) = 2 AND extract(day FROM r) = 29 THEN 28 ELSE extract(day FROM r)::int END) AS rs,
    make_date(extract(year FROM c)::int + 1, extract(month FROM r)::int,
              CASE WHEN extract(month FROM r) = 2 AND extract(day FROM r) = 29 THEN 28 ELSE extract(day FROM r)::int END) AS rn
  FROM b
), g AS (
  SELECT f.*, CASE WHEN abs(rs - c) <= 90 AND abs(rs - c) <= abs(rn - c) THEN rs
                   WHEN abs(rn - c) <= 90 THEN rn END AS rf
  FROM f
)
SELECT id AS atom_id, agent_id, remembered_on AS old_remembered_on,
       (extract(year FROM rf) - extract(year FROM r))::int AS years_shift,
       remembered_on + make_interval(years => (extract(year FROM rf) - extract(year FROM r))::int) AS new_remembered_on,
       CASE WHEN year_in_text THEN 2 ELSE 1 END AS tier, iso_in_text
FROM g
WHERE rf IS NOT NULL AND extract(year FROM rf) - extract(year FROM r) BETWEEN 1 AND 3;
