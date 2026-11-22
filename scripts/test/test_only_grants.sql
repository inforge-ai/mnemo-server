-- Test/CI only: extra privileges the suite's per-test cleanup (tests/conftest.py
-- _CLEAN) needs beyond production's app-role grants. Run as mnemo_migrator
-- after schema.sql + mnemo migrate. Do NOT apply to production.
GRANT DELETE ON decomposer_usage TO mnemo;
