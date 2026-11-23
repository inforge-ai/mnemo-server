-- Dry run of the wrong-year remembered_on repair. Read-only (rolls back).
--   psql "$DB" -f scripts/phase1/remembered_on_dryrun.sql
BEGIN;
\ir remembered_on_candidates.sql
SELECT ag.name AS agent, c.tier, c.years_shift, count(*) AS atoms,
       count(*) FILTER (WHERE c.iso_in_text) AS iso_date_in_text,
       min(c.new_remembered_on - c.old_remembered_on) AS min_shift, max(c.new_remembered_on - c.old_remembered_on) AS max_shift
FROM cand c JOIN agents ag ON ag.id = c.agent_id
GROUP BY ROLLUP(ag.name, c.tier, c.years_shift)
HAVING ag.name IS NOT NULL AND c.tier IS NOT NULL AND c.years_shift IS NOT NULL
    OR ag.name IS NULL
ORDER BY ag.name NULLS LAST, c.tier, c.years_shift;
SELECT tier, count(*) AS would_update FROM cand GROUP BY ROLLUP(tier) ORDER BY tier NULLS LAST;
\if :{?agents}
\if :{?same_year_only}
\else
\set same_year_only false
\endif
SELECT 'scope' AS q, :'agents' AS agents, :'same_year_only' AS same_year_only, agent_name, count(*) AS would_update_tier1
FROM cand WHERE tier = 1 AND agent_name = ANY(string_to_array(:'agents', ','))
  AND (NOT :'same_year_only'::boolean OR same_year)
GROUP BY ROLLUP(agent_name) ORDER BY agent_name NULLS LAST;
\endif
ROLLBACK;
