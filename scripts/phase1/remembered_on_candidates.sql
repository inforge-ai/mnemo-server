-- Shared by the remembered_on repair scripts. Builds temp table `cand`: active
-- atoms whose remembered_on looks like a wrong-year resolution.
--   rule: remembered_on >= 300 days before created_at; putting the month-day into
--         created_at's year (or the next year) lands within 90 days of created_at;
--         the shift is 1-3 years (e.g. "21 Jan" written in Nov 2026 stored as
--         2024-01-21 → nearest 2027-01-21).
--   same_year:  the nearest corrected date is in created_at's own year.
--   new_remembered_on:  the nearest corrected date (target "nearest").
--   past_remembered_on: the same month-day in created_at's own year, i.e. the
--                       occurrence just past for a following-year case (target "past").
--   tier 1: the wrong year does not appear in the atom text (date-only fix is consistent).
--   tier 2: the wrong year appears in the text; see remembered_on_tier2_*.
-- Atoms already in either undo table (and not reverted) are excluded, so a
-- repaired or flagged atom is never picked up again.
-- Reads atoms; writes only the temp table (plus the undo tables' DDL, which a
-- dry run rolls back). Counts and ids only; no text leaves the DB.
SET timezone = 'UTC';
CREATE TABLE IF NOT EXISTS repair_remembered_on_2026_11 (
  atom_id            uuid PRIMARY KEY REFERENCES atoms(id) ON DELETE CASCADE,
  agent_id           uuid NOT NULL,
  old_remembered_on  timestamptz NOT NULL,
  new_remembered_on  timestamptz NOT NULL,
  tier               int NOT NULL,
  repaired_at        timestamptz NOT NULL DEFAULT now(),
  reverted_at        timestamptz
);
CREATE TABLE IF NOT EXISTS repair_date_text_2026_11 (
  atom_id            uuid PRIMARY KEY REFERENCES atoms(id) ON DELETE CASCADE,
  agent_id           uuid NOT NULL,
  action             text NOT NULL CHECK (action IN ('replace', 'flag')),
  old_remembered_on  timestamptz NOT NULL,
  new_remembered_on  timestamptz,          -- replace only
  old_text           text,                 -- replace only
  new_text           text,                 -- replace only
  old_embedding      vector,               -- replace only
  new_embedding      vector,               -- replace only; filled by remembered_on_tier2_embed.py
  old_verification   jsonb,
  planned_at         timestamptz NOT NULL DEFAULT now(),
  applied_at         timestamptz,
  reverted_at        timestamptz
);
CREATE TEMP TABLE cand ON COMMIT DROP AS
WITH b AS (
  SELECT a.id, a.agent_id, (SELECT name FROM agents WHERE id = a.agent_id) AS agent_name,
         a.remembered_on, a.created_at, a.created_at::date AS c, a.remembered_on::date AS r,
         strpos(a.text_content, to_char(a.remembered_on::date, 'YYYY-MM-DD')) > 0 AS iso_in_text,
         a.text_content ~ ('\m' || extract(year FROM a.remembered_on)::int || '\M') AS year_in_text
  FROM atoms a
  WHERE a.is_active AND a.remembered_on <= a.created_at - interval '300 days'
    AND NOT EXISTS (SELECT 1 FROM repair_remembered_on_2026_11 u WHERE u.atom_id = a.id AND u.reverted_at IS NULL)
    AND NOT EXISTS (SELECT 1 FROM repair_date_text_2026_11 u WHERE u.atom_id = a.id AND u.reverted_at IS NULL)
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
SELECT id AS atom_id, agent_id, agent_name, remembered_on AS old_remembered_on, created_at,
       extract(year FROM rf) = extract(year FROM c) AS same_year,   -- nearest corrected date in created_at's own year
       (extract(year FROM rf) - extract(year FROM r))::int AS years_shift,
       remembered_on + make_interval(years => (extract(year FROM rf) - extract(year FROM r))::int) AS new_remembered_on,
       remembered_on + make_interval(years => (extract(year FROM c) - extract(year FROM r))::int) AS past_remembered_on,
       CASE WHEN year_in_text THEN 2 ELSE 1 END AS tier, iso_in_text
FROM g
WHERE rf IS NOT NULL AND extract(year FROM rf) - extract(year FROM r) BETWEEN 1 AND 3;
