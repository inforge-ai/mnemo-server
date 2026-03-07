# Mnemo - Local API Key Auth Layer

## For: Claude Code / Implementation Agent
## Status: READY TO BUILD
## Priority: High — fixes agent identity for dogfood testing, enables multi-user production
## Estimated time: 2-3 hours
## Prerequisite: None — uses existing mnemo Postgres

---

## Context

Agent identity persistence is now fixed by name-based lookup (commit 08fbbf7).
The remaining gap is API key auth for production: without it, any caller can
read or write any agent's memories. We also want stable agent registration that
survives schema migrations and test runs without orphaning memories.

This spec replaces the Supabase auth spec. Everything lives in the existing
mnemo Postgres — no external services.

---

## What Changes

- `api_keys` table added to existing schema.sql
- UNIQUE constraint added to `agents.name`
- New auth service: key generation, hashing, validation
- New FastAPI dependency: `get_current_agent`
- New auth endpoints: `POST /v1/auth/register`, `GET /v1/auth/me`
- `MNEMO_AUTH_ENABLED` feature flag (default false — zero breakage during dev)
- `MnemoClient` gains optional `api_key` constructor param
- MCP server uses `MNEMO_API_KEY` env var; resolves identity via `/v1/auth/me`
- CLI: `mnemo register`, `mnemo new-key`, `mnemo list-agents`
- Migration script for existing agents

---

## Schema Changes

### Add to schema.sql

```sql
-- Unique agent names (defensive — prevents duplicate identity on restart)
ALTER TABLE agents ADD CONSTRAINT agents_name_unique UNIQUE (name);

-- API keys (hashed — plaintext never stored)
CREATE TABLE api_keys (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    key_hash     TEXT NOT NULL,
    key_prefix   TEXT NOT NULL,        -- first 16 chars for display
    name         TEXT DEFAULT 'default',
    created_at   TIMESTAMPTZ DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    is_active    BOOLEAN DEFAULT true
);

CREATE INDEX idx_api_keys_hash  ON api_keys(key_hash);
CREATE INDEX idx_api_keys_agent ON api_keys(agent_id);

-- Grants for mnemo user
GRANT SELECT, INSERT, UPDATE ON api_keys TO mnemo;
```

Apply with:
```bash
sudo -u postgres psql mnemo -f schema.sql
```

Or just the new parts:
```bash
sudo -u postgres psql mnemo -c "
  ALTER TABLE agents ADD CONSTRAINT agents_name_unique UNIQUE (name);
  CREATE TABLE api_keys ( ... );
  GRANT SELECT, INSERT, UPDATE ON api_keys TO mnemo;
"
```

---

## Key Format

```
mnemo_<32 bytes of secrets.token_urlsafe>
```

Example: `mnemo_Kx9mP2rQvL8nZjYtFwBd3eHsUcA6oN1gIqRkMpVy`

- Prefix `mnemo_` for easy identification
- 32 bytes urlsafe base64 → ~43 chars after prefix
- Store SHA-256 hash in `key_hash`, first 16 chars in `key_prefix`
- Key returned to caller exactly once at creation; never stored or returned again

---

## Auth Service

New file: `mnemo/server/services/auth_service.py`

### Functions

```python
def generate_api_key() -> str:
    """Generate a new mnemo_ prefixed API key."""

def hash_key(key: str) -> str:
    """Return SHA-256 hex digest of the key."""

async def create_agent_with_key(
    conn, name: str, persona: str, domain_tags: list[str], key_name: str = "default"
) -> tuple[dict, str]:
    """
    Idempotent: if agent with this name exists, generates a new key for it.
    If agent does not exist, creates it and generates a key.
    Returns (agent_dict, plaintext_key).
    Inserts key hash into api_keys table.
    """

async def validate_api_key(conn, key: str) -> dict | None:
    """
    Look up key by SHA-256 hash. Update last_used_at if found.
    Returns agent row dict if key is valid and active, else None.
    """

async def create_additional_key(
    conn, agent_id: UUID, key_name: str = "default"
) -> str:
    """Generate and store an additional key for an existing agent. Returns plaintext key."""
```

---

## FastAPI Auth Dependency

New file: `mnemo/server/auth.py`

```python
from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer(auto_error=False)

async def get_current_agent(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> dict:
    """
    Validates Bearer token against api_keys table.
    Returns agent dict on success.
    Raises 401 if MNEMO_AUTH_ENABLED and key is missing or invalid.
    If MNEMO_AUTH_ENABLED=false, returns a sentinel agent dict with id=None
    so existing routes work unchanged.
    """
```

Usage in routes (added to every endpoint when auth is enabled):

```python
@router.post("/agents/{agent_id}/remember")
async def remember(agent_id: UUID, body: RememberRequest, agent=Depends(get_current_agent)):
    if agent["id"] and agent["id"] != agent_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    ...
```

---

## Auth Endpoints

New file: `mnemo/server/routes/auth.py`

### POST /v1/auth/register

Request:
```json
{
  "name": "claude-desktop",
  "persona": "coding assistant",
  "domain_tags": ["python", "systems"],
  "key_name": "default"
}
```

Response (201):
```json
{
  "agent_id": "5b4cf0c0-...",
  "name": "claude-desktop",
  "api_key": "mnemo_Kx9mP2rQvL8...",
  "message": "Save this key — it will not be shown again."
}
```

Behaviour:
- If agent name exists: create new key, return existing agent_id + new key
- If agent name is new: create agent + key, return both
- API key is returned once only in this response

### GET /v1/auth/me

Headers: `Authorization: Bearer mnemo_...`

Response (200):
```json
{
  "agent_id": "5b4cf0c0-...",
  "name": "claude-desktop",
  "persona": "coding assistant",
  "domain_tags": ["python", "systems"],
  "key_prefix": "mnemo_Kx9mP2rQvL8",
  "last_used_at": "2026-03-04T16:00:00Z"
}
```

Raises 401 if key is missing or invalid. Always active (no flag check).

---

## Feature Flag

`MNEMO_AUTH_ENABLED` (default: `false`)

When `false`:
- All existing endpoints work without any `Authorization` header
- `get_current_agent` returns sentinel `{"id": None}` — no 401s raised
- `/v1/auth/register` and `/v1/auth/me` still work (useful for generating keys before enabling)

When `true`:
- All endpoints require valid Bearer token
- Agent ID in URL path must match authenticated agent (else 403)
- 401 on missing or invalid key

This allows generating keys and configuring all agents before flipping the flag.

---

## MnemoClient Changes

Constructor gains optional `api_key` param:

```python
MnemoClient(base_url, api_key=None)
```

When `api_key` is set, every request includes:
```
Authorization: Bearer mnemo_...
```

New convenience methods:
```python
async def register_with_key(self, name, persona=None, domain_tags=None) -> tuple[str, str]:
    """Returns (agent_id, api_key)."""

async def me(self) -> dict:
    """GET /v1/auth/me — returns current agent info."""
```

---

## MCP Server Changes

### New environment variables

```
MNEMO_API_KEY=mnemo_...    # API key (replaces MNEMO_AGENT_ID)
MNEMO_AGENT_NAME=claude-desktop  # used only if MNEMO_API_KEY not set
```

`MNEMO_AGENT_ID` is deprecated but kept for one release as a fallback.

### Startup flow

```
If MNEMO_API_KEY is set:
    client = MnemoClient(base_url, api_key=MNEMO_API_KEY)
    agent_info = await client.me()          # validates key, gets agent_id
    _agent_id = agent_info["agent_id"]
    log: "Authenticated as {name} ({agent_id})"

Else:
    client = MnemoClient(base_url)          # no auth header
    _agent_id = await _resolve_agent(client)  # existing name-based lookup
    log: "Running without auth (set MNEMO_API_KEY for production)"
```

Remove MNEMO_AGENT_ID from new documentation. Keep in code as silent fallback.

---

## CLI

New file: `mnemo/cli.py`

Entry point in `pyproject.toml`:
```toml
[project.scripts]
mnemo = "mnemo.cli:cli"
```

### Commands

```bash
mnemo register <name> [--persona "..."] [--tags "python,systems"] [--key-name "default"]
```
Creates agent (or adds key to existing), prints key once. Example output:
```
Agent   : claude-desktop
ID      : 5b4cf0c0-7f36-432a-9037-7f0d9afd4fb7
API Key : mnemo_Kx9mP2rQvL8nZjYtFwBd3eHsUcA6oN1gIqRkMpVy

Save this key — it will not be shown again.
Add to your MCP config or service file:
  MNEMO_API_KEY=mnemo_Kx9mP2rQvL8nZjYtFwBd3eHsUcA6oN1gIqRkMpVy
```

```bash
mnemo new-key <name> [--key-name "laptop"]
```
Generates additional key for existing agent. Same output format.

```bash
mnemo list-agents
```
Prints table of all active agents with key counts and last_used_at.

```bash
mnemo whoami --api-key mnemo_...
```
Calls GET /v1/auth/me, prints agent info. Useful for verifying a key works.

---

## Migration Script

`mnemo/scripts/migrate_to_auth.py`

For each existing agent in the core database:
1. Call `POST /v1/auth/register` with the agent's name (idempotent — creates key, reuses agent)
2. Print the API key to stdout with agent name and ID
3. Exit with instructions to update service files

Usage:
```bash
uv run python -m mnemo.scripts.migrate_to_auth
```

Output:
```
Generating API keys for existing agents...

alice-py-dev   (4a9e7da0-...) : mnemo_AbCdEf...
claude-desktop (5b4cf0c0-...) : mnemo_GhIjKl...
test-agent     (603634ef-...) : mnemo_MnOpQr...

Done. Update your service files with MNEMO_API_KEY=<key>.
Enable auth when ready: MNEMO_AUTH_ENABLED=true
```

---

## Build Order

1. Schema: add `api_keys` table + UNIQUE constraint on agents.name (~10 min)
2. `auth_service.py`: key generation, hashing, validation, create_agent_with_key (~30 min)
3. `auth.py`: `get_current_agent` FastAPI dependency + feature flag logic (~20 min)
4. `routes/auth.py`: register + me endpoints (~20 min)
5. Wire auth route into `main.py` (~5 min)
6. Apply `Depends(get_current_agent)` to all existing route endpoints (~20 min)
7. `MnemoClient`: api_key param + auth header + register_with_key + me (~20 min)
8. MCP server: MNEMO_API_KEY startup flow (~20 min)
9. `cli.py`: register, new-key, list-agents, whoami commands (~30 min)
10. `scripts/migrate_to_auth.py` (~15 min)
11. Update existing tests: pass api_key through test client where needed (~20 min)
12. Run migration, generate keys for existing 3 agents (~5 min)
13. Full regression: `uv run pytest tests/ -v` (~5 min)

Total estimated: 2-3 hours

---

## Verification

1. `mnemo register test-auth` → get API key
2. `mnemo whoami --api-key mnemo_...` → see agent info
3. Set `MNEMO_API_KEY` in MCP service config
4. Restart MCP server → connects as same agent, memories intact
5. `mnemo_stats` via Claude → correct agent, correct count
6. `MNEMO_AUTH_ENABLED=false` → all existing tests pass without keys
7. `MNEMO_AUTH_ENABLED=true` → request without key → 401
8. `MNEMO_AUTH_ENABLED=true` → wrong key → 401
9. `MNEMO_AUTH_ENABLED=true` → access another agent's data → 403
10. `uv run pytest tests/ -v` → all green

---

## What NOT To Build

- Row-level security in Postgres (app-layer is sufficient)
- OAuth / JWT / SSO (API keys are right for machine-to-machine)
- User accounts (agents are the identity primitive)
- Rate limiting (not needed until public)
- Key rotation automation (manual via CLI is fine for now)
- Key expiry / TTL (add when needed)
