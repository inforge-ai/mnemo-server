-- Dry run of the tier-1 remembered_on repair. Read-only (rolls back).
--   psql "$DB" -f scripts/phase1/remembered_on_dryrun.sql                      # all candidates
--   psql "$DB" -v agents=clio -v which=next -v target=past -f ...             # plus one scope
\set ON_ERROR_STOP on
BEGIN;
\ir remembered_on_candidates.sql
SELECT agent_name AS agent, tier, same_year, years_shift, count(*) AS atoms,
       count(*) FILTER (WHERE iso_in_text) AS iso_date_in_text
FROM cand GROUP BY ROLLUP(agent_name, tier, same_year, years_shift)
HAVING (agent_name IS NULL) OR years_shift IS NOT NULL
ORDER BY agent_name NULLS LAST, tier, same_year, years_shift;
SELECT tier, count(*) AS candidates FROM cand GROUP BY ROLLUP(tier) ORDER BY tier NULLS LAST;
\if :{?agents}
\ir remembered_on_scope.sql
SELECT 'scope' AS q, :'agents' AS agents, :'which' AS which, :'target' AS target,
       agent_name, count(*) AS would_update FROM scope GROUP BY ROLLUP(agent_name) ORDER BY agent_name NULLS LAST;
SELECT agent_name AS agent, old_remembered_on::date AS old_date, new_remembered_on::date AS new_date, count(*) AS atoms
FROM scope GROUP BY 1,2,3 ORDER BY 1,2,3;
\endif
ROLLBACK;
