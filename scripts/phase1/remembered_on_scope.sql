-- Shared: the tier-1 scope for dry run and exec. Needs -v agents=a,b
-- -v which=same|next|all (nearest corrected date in created_at's own year, the
-- following year, or both) -v target=nearest|past (new date = the nearest
-- correction, or the same month-day in created_at's own year).
\if :{?agents}
\else
  \echo 'usage: -v agents=a,b -v which=same|next|all -v target=nearest|past [-v expected=N]'
  DO $$ BEGIN RAISE EXCEPTION 'missing scope argument'; END $$;
\endif
\if :{?which}
\else
  \echo 'usage: -v agents=a,b -v which=same|next|all -v target=nearest|past [-v expected=N]'
  DO $$ BEGIN RAISE EXCEPTION 'missing scope argument'; END $$;
\endif
\if :{?target}
\else
  \echo 'usage: -v agents=a,b -v which=same|next|all -v target=nearest|past [-v expected=N]'
  DO $$ BEGIN RAISE EXCEPTION 'missing scope argument'; END $$;
\endif
SELECT :'which' IN ('same', 'next', 'all') AND :'target' IN ('nearest', 'past') AS args_ok \gset
\if :args_ok
\else
  \echo 'bad -v which / -v target'
  DO $$ BEGIN RAISE EXCEPTION 'bad scope argument'; END $$;
\endif
CREATE TEMP TABLE scope ON COMMIT DROP AS
SELECT atom_id, agent_id, agent_name, old_remembered_on, tier,
       CASE WHEN :'target' = 'past' THEN past_remembered_on ELSE new_remembered_on END AS new_remembered_on
FROM cand
WHERE tier = 1 AND agent_name = ANY(string_to_array(:'agents', ','))
  AND (:'which' = 'all' OR (:'which' = 'same') = same_year)
  -- a "past" date must not be after the atom was written
  AND (:'target' = 'nearest' OR past_remembered_on <= created_at);
