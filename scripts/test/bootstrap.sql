-- Test/CI database bootstrap. Run as a superuser against the `postgres` database.
-- Mirrors production roles: mnemo_migrator owns the schema, mnemo has DML only.
-- Test-only passwords; never use these anywhere else.
CREATE ROLE mnemo LOGIN PASSWORD 'mnemo_test_pw';
CREATE ROLE mnemo_migrator LOGIN PASSWORD 'mnemo_migrator_test_pw';
CREATE DATABASE mnemo_test OWNER mnemo_migrator;
\connect mnemo_test
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
GRANT CONNECT ON DATABASE mnemo_test TO mnemo;
GRANT USAGE ON SCHEMA public TO mnemo;
