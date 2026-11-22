-- v2 Phase 0: one-off superuser script — create the mnemo_migrator role and
-- make it owner of mnemo's schema objects, so `mnemo migrate` can run DDL
-- without a superuser connection.
--
-- Run as a superuser against the production database, e.g.:
--   sudo -u postgres psql -d mnemo -v ON_ERROR_STOP=1 -f scripts/phase0_create_migrator_role.sql
-- psql prompts for the new role's password (not echoed, not in shell history).
--
-- Then add to .env (never commit it):
--   MNEMO_MIGRATION_DATABASE_URL=postgresql://mnemo_migrator:<password>@localhost:5432/mnemo
--
-- What it changes:
--   - creates LOGIN role mnemo_migrator (NOSUPERUSER, NOCREATEDB, NOCREATEROLE)
--   - transfers ownership of every table, sequence, view and function in
--     schema public that postgres owns, EXCEPT objects belonging to an
--     extension (vector, uuid-ossp stay with postgres)
--   - grants CREATE on schema public (new tables in future migrations)
-- Existing grants to the mnemo app role are kept: ALTER ... OWNER TO keeps
-- the ACL. The app role itself is not changed.
-- Safe to re-run.

\prompt 'Password for new role mnemo_migrator: ' migrator_password

SELECT format('CREATE ROLE mnemo_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'migrator_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mnemo_migrator')
\gexec

SELECT format('ALTER ROLE mnemo_migrator LOGIN PASSWORD %L', :'migrator_password')
\gexec

BEGIN;

SELECT format('GRANT CONNECT ON DATABASE %I TO mnemo_migrator', current_database())
\gexec
GRANT USAGE, CREATE ON SCHEMA public TO mnemo_migrator;

-- Tables, views, sequences, materialized views (not extension members).
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.oid, c.relname, c.relkind
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
      AND pg_get_userbyid(c.relowner) = 'postgres'
      AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e'
      )
      -- sequences owned by a table column move with the table
      AND NOT (c.relkind = 'S' AND EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype IN ('a', 'i')
      ))
  LOOP
    EXECUTE format(
      'ALTER %s public.%I OWNER TO mnemo_migrator',
      CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                     WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
      r.relname
    );
  END LOOP;
END $$;

-- Functions (effective_confidence, revoke_agent_capabilities, ...), not
-- extension members.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT p.oid
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND pg_get_userbyid(p.proowner) = 'postgres'
      AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
      )
  LOOP
    EXECUTE format('ALTER ROUTINE %s OWNER TO mnemo_migrator', r.oid::regprocedure);
  END LOOP;
END $$;

COMMIT;

-- Report: what mnemo_migrator owns now, and anything left with postgres.
SELECT 'owned by mnemo_migrator' AS what, count(*)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND pg_get_userbyid(c.relowner) = 'mnemo_migrator' AND c.relkind IN ('r','p','v','m','S')
UNION ALL
SELECT 'functions owned by mnemo_migrator', count(*)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND pg_get_userbyid(p.proowner) = 'mnemo_migrator'
UNION ALL
SELECT 'non-extension tables still owned by postgres', count(*)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r','p') AND pg_get_userbyid(c.relowner) = 'postgres'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e');
