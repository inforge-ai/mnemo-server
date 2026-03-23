# Mnemo Admin Spec

**Purpose:** Implementation spec for Claude Code. Covers admin CLI, admin API endpoints, operator/agent lifecycle, trust management, and the Stripe-gated onboarding flow.

**Repo:** `/home/mnemo/mnemo-server` on mnemo-net
**Runtime:** Postgres 16 + pgvector (host systemd), Mnemo server in Docker, Caddy reverse proxy

---

## 1. Data Model

### 1.1 Hierarchy

```
Platform (Mnemo)
  └─ Operator (a paying customer — person or org)
       ├─ has: username, org, email, API key, Stripe IDs
       ├─ status: active | suspended | cancelled
       └─ Agent (one or many per operator)
            ├─ has: UUID (primary key), address, display_name, type, status
            ├─ address format: {agent_name}:{username}.{org}
            │   e.g. clio:tom.inforge, analyst:nels.inforge
            └─ status: active | departed
```
The addresses are agent_id:op_id.org

One issue we will need to address, is how the api works.  I believe currently it
uses agent uuid, while this spec assumes agent address, i think uuid is best 
for api http(s) endpoints.  

It looks like v1 is droppped from the endpoints for this entire spec, so please be mindful of that.  

### 1.2 Schema Changes

#### `operators` table (new columns or new table if not yet present)

```sql
CREATE TABLE IF NOT EXISTS operators (
    uuid            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username        VARCHAR(32) NOT NULL UNIQUE,   -- lowercase, alphanumeric + hyphens, 3-32 chars
    org             VARCHAR(32) NOT NULL,           -- same validation as username
    display_name    VARCHAR(128) NOT NULL,
    email           VARCHAR(256) NOT NULL UNIQUE,
    api_key_hash    VARCHAR(128) NOT NULL,          -- bcrypt or argon2 hash, never store plaintext
    status          VARCHAR(16) NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'suspended', 'cancelled')),
    stripe_customer_id    VARCHAR(64),              -- nullable: admin operator won't have one
    stripe_subscription_id VARCHAR(64),             -- nullable
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_operators_org ON operators(org);
CREATE INDEX idx_operators_status ON operators(status);
```

**Validation rules for `username` and `org`:**
- Lowercase only
- Alphanumeric + hyphens
- 3–32 characters
- Must start with a letter
- No consecutive hyphens
- Regex: `^[a-z][a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$`

#### `agents` table (confirm existing columns, add if missing)

```sql
-- These columns should already exist; verify and add any missing:
--   uuid, display_name, agent_type, capabilities, health_endpoint,
--   status, operator_uuid, metadata, address

-- Ensure status supports 'departed':
ALTER TABLE agents
    DROP CONSTRAINT IF EXISTS agents_status_check,
    ADD CONSTRAINT agents_status_check
        CHECK (status IN ('active', 'departed'));

-- Ensure address is populated and unique:
ALTER TABLE agents
    ADD CONSTRAINT agents_address_unique UNIQUE (address);
```

#### `platform_config` table (new — for global flags)

```sql
CREATE TABLE IF NOT EXISTS platform_config (
    key     VARCHAR(64) PRIMARY KEY,
    value   JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed the sharing toggle
INSERT INTO platform_config (key, value)
VALUES ('sharing_enabled', 'true'::jsonb)
ON CONFLICT (key) DO NOTHING;
```

### 1.3 API Key Generation
I beleive this already works, so no need to reinvent the wheel.  

On operator creation, generate a secure random API key:

```python
import secrets
raw_key = f"mnemo_{secrets.token_urlsafe(32)}"  # e.g. mnemo_a3Bf9x...
# Return raw_key to the operator ONCE (in the response or email)
# Store only the hash
api_key_hash = hash_api_key(raw_key)  # bcrypt or argon2id
```

The `mnemo_` prefix makes keys greppable in logs and distinguishable from other secrets.

---

## 2. Authentication Model

Three auth levels:

| Level | Header | Who | Can do |
|-------|--------|-----|--------|
| **Admin** | `X-Admin-Token` | Tom (platform operator) | Everything below + operator CRUD, global trust toggle, agent depart/reinstate |
| **Operator** | `X-API-Key` | Paying customers | Register/manage their own agents, their agents remember/recall/share |
| **Agent** | (inherits from operator key) | Individual agents | remember, recall, share (scoped to own identity) |

The admin token is a single static secret from the environment (`MNEMO_ADMIN_TOKEN`).

Operator API keys authenticate via the existing `X-API-Key` header. The server resolves the key to an operator, and all agent operations are scoped to that operator.

**Auth middleware logic:**

```python
def resolve_auth(request):
    admin_token = request.headers.get("X-Admin-Token")
    if admin_token and verify_admin_token(admin_token):
        return AuthContext(role="admin", operator=None)

    api_key = request.headers.get("X-API-Key")
    if api_key:
        operator = lookup_operator_by_key(api_key)
        if operator and operator.status == "active":
            return AuthContext(role="operator", operator=operator)

    raise HTTPException(401, "Invalid or missing credentials")
```

---

## 3. Health Endpoint

### `GET /health`

**No auth required.** Answers one question: "is Mnemo up and working?"

```json
// Response 200
{
    "status": "ok",                    // "ok" | "degraded" | "down"
    "version": "0.4.2",               // from pyproject.toml or __version__
    "schema_version": "003",           // from migration table
    "uptime_seconds": 84321,
    "postgres": "ok"                   // connectivity check
}
```

**Implementation notes:**

We haven't used alembic, and i'm not sure if this feature is worth adding the package
for, so please discuss whether you think it's worth it now. 

- `version`: Read from `mnemo.__version__` or `importlib.metadata.version("mnemo-server")`, set at build/release time in `pyproject.toml`.
- `schema_version`: Query the migration tracking table. If using Alembic: `SELECT version_num FROM alembic_version`. If using raw SQL migrations: `SELECT MAX(version) FROM schema_migrations`.
- `postgres`: A simple `SELECT 1` connectivity check. If it fails, set `"status": "degraded"` and `"postgres": "unreachable"`.
- `uptime_seconds`: Track `app_start_time = time.time()` at FastAPI startup, compute delta.
- The health endpoint should be fast — no heavy queries, no auth overhead.
- **No counts, no config flags.** The public endpoint leaks nothing about platform scale or operational state beyond "it's up."

### `GET /health/detailed`

**Admin-only** (requires `X-Admin-Token`). Full diagnostic view including platform scale and config.

```json
// Response 200
{
    // ...everything from /health, plus:
    "sharing_enabled": true,
    "operator_count": 2,
    "agent_count": 5,
    "atom_count": 1847,
    "embedding_model": "thenlper/gte-small",
    "embedding_dimensions": 384,
    "postgres_version": "16.2",
    "pgvector_version": "0.7.0",
    "docker_image": "mnemo-server:latest",
    "config": {
        "sharing_enabled": true,
        "min_similarity": 0.75,
        "decomposer": "haiku"
    }
}
```

This gives you a single curl to diagnose what's running on mnemo-net without SSHing in:
```bash
curl -H "X-Admin-Token: $MNEMO_ADMIN_TOKEN" https://api.mnemo-ai.com/health/detailed
```

---

## 4. Admin API Endpoints


All admin endpoints are under `/admin/` and require `X-Admin-Token`.

### 4.1 Operator Management

#### `POST /admin/operators`
Create a new operator (used by Stripe webhook or manual admin creation).

```json
// Request
{
    "username": "tom",
    "org": "inforge",
    "display_name": "Tom Davis",
    "email": "tom@inforge.com",
    "stripe_customer_id": null,        // optional
    "stripe_subscription_id": null     // optional
}

// Response 201
{
    "uuid": "...",
    "username": "tom",
    "org": "inforge",
    "display_name": "Tom Davis",
    "email": "tom@inforge.com",
    "api_key": "mnemo_a3Bf9x...",      // ONLY returned once, at creation
    "address_namespace": "tom.inforge",
    "status": "active",
    "created_at": "..."
}
```

#### `GET /admin/operators`
List all operators.

```json
// Response 200
{
    "operators": [
        {
            "uuid": "...",
            "username": "tom",
            "org": "inforge",
            "display_name": "Tom Davis",
            "email": "tom@inforge.com",
            "status": "active",
            "agent_count": 3,
            "created_at": "..."
        }
    ]
}
```

#### `GET /admin/operators/{username}`
Get single operator detail.

#### `POST /admin/operators/{username}/suspend`
Suspend an operator. Sets status to `suspended`. All their agents are departed.

```json
// Response 200
{
    "username": "tom",
    "status": "suspended",
    "agents_departed": 3
}
```

#### `POST /admin/operators/{username}/reinstate`
Reinstate a suspended operator. Sets status to `active`. Agents remain departed — must be individually reinstated (intentional: gives admin control over which agents come back).

```json
// Response 200
{
    "username": "tom",
    "status": "active",
    "note": "Agents remain departed. Reinstate individually via /admin/agents/{address}/reinstate"
}
```

#### `POST /admin/operators/{username}/rotate-key`
Rotate an operator's API key. Invalidates the old key immediately.

```json
// Response 200
{
    "username": "tom",
    "api_key": "mnemo_newKey..."    // returned once
}
```

### 4.2 Agent Management

#### `GET /admin/agents`
List all agents. Optional query params: `?operator={username}`, `?status=active|departed`

```json
// Response 200
{
    "agents": [
        {
            "uuid": "...",
            "address": "clio:tom.inforge",
            "display_name": "Clio",
            "agent_type": "interactive",
            "status": "active",
            "operator_username": "tom",
            "created_at": "..."
        }
    ]
}
```

#### `POST /admin/agents/{address}/depart`
Soft-disable an agent. Status → `departed`. Agent cannot remember/recall. Existing memories preserved.

```json
// Response 200
{
    "address": "clio:tom.inforge",
    "status": "departed"
}
```

#### `POST /admin/agents/{address}/reinstate`
Re-enable a departed agent. Only works if the parent operator is `active`.

```json
// Response 200
{
    "address": "clio:tom.inforge",
    "status": "active"
}

// Response 409 (if operator is suspended/cancelled)
{
    "error": "Cannot reinstate agent: operator 'tom' is suspended"
}
```

### 4.3 Trust / Sharing Management

#### `GET /admin/trust/status`
Check global sharing toggle.

```json
// Response 200
{ "sharing_enabled": true }
```

#### `POST /admin/trust/disable`
Disable sharing globally. No new shares can be created. Existing shares are suspended (not deleted). `recall_shared` returns empty results while disabled.

```json
// Response 200
{
    "sharing_enabled": false,
    "note": "Existing shares suspended, not deleted. Enable to restore."
}
```

#### `POST /admin/trust/enable`
Re-enable sharing globally.

#### `GET /admin/trust/shares`
List active shares. Optional query params: `?operator={username}`, `?agent={address}`

```json
// Response 200
{
    "shares": [
        {
            "capability_id": "...",
            "grantor_address": "clio:tom.inforge",
            "grantee_address": "nels-claude:nels.inforge",
            "name": "project-context",
            "created_at": "...",
            "atom_count": 20
        }
    ]
}
```

#### `DELETE /admin/trust/shares/{capability_id}`
Admin override revoke. Soft-deletes the share.

---

## 5. CLI Commands

The CLI mirrors the admin API 1:1. All commands require the admin token (from env `MNEMO_ADMIN_TOKEN` or `--admin-token` flag).

```
mnemo admin operator create --username tom --org inforge --display-name "Tom Davis" --email tom@inforge.com
mnemo admin operator list
mnemo admin operator show <username>
mnemo admin operator suspend <username>
mnemo admin operator reinstate <username>
mnemo admin operator rotate-key <username>

mnemo admin agent list [--operator <username>] [--status active|departed]
mnemo admin agent depart <address>
mnemo admin agent reinstate <address>

mnemo admin trust status
mnemo admin trust disable
mnemo admin trust enable
mnemo admin trust list [--operator <username>] [--agent <address>]
mnemo admin trust revoke <capability_id>
```

**Output format:** JSON by default (machine-parseable), `--pretty` for human-readable tables.

**CLI implementation notes:**
- Use `click` (already in the project?) or `typer`
- Each command is a thin wrapper: parse args → HTTP call to admin endpoint → format response
- The CLI talks to the API, not directly to the DB. This ensures all validation and side-effects go through one code path.

---

## 6. Stripe Onboarding Flow (Deferred to Week 2)

> This section is included for design completeness. Do NOT implement this week.
> Launch with manual operator creation via admin CLI.

### 6.1 Sequence

```
User visits mnemo-ai.com
  → clicks "Start Paid Beta" (£5/mo)
  → fills details form: email, display_name, username, org
  → client validates username/org format
  → client calls POST /onboarding/checkout with details
  → server creates Stripe Checkout Session (details in metadata)
  → server returns Stripe Checkout URL
  → user redirects to Stripe → enters CC → completes payment
  → Stripe fires webhook: checkout.session.completed
  → server webhook handler:
      1. Extract metadata (username, org, email, display_name)
      2. POST /admin/operators (internal call) with stripe_customer_id + stripe_subscription_id
      3. Send welcome email with API key
  → user lands on success page
  → success page: "Register your first agent" form
      → POST /v1/agents (with their new API key)
      → returns UUID + address
```

### 6.2 Endpoints (Week 2)

```
POST /onboarding/checkout     — creates Stripe session, returns redirect URL
POST /webhooks/stripe         — handles checkout.session.completed, subscription.deleted
GET  /onboarding/success      — post-payment landing page
```

### 6.3 Subscription Lifecycle Webhooks

| Stripe Event | Action |
|-------------|--------|
| `checkout.session.completed` | Create operator, generate API key, send welcome email |
| `customer.subscription.updated` | Update status if needed (e.g. payment failed → suspend) |
| `customer.subscription.deleted` | Set operator status to `cancelled`, depart all agents |
| `invoice.payment_failed` | (Optional) Send warning email, grace period before suspend |

---

## 7. Enforcement Points

The admin status flags need to be checked at the right points in existing code:

### 7.1 Operator Status Check
Every authenticated request must verify the operator is `active`. Add to auth middleware:

```python
if operator.status != "active":
    raise HTTPException(403, f"Operator account is {operator.status}")
```

### 7.2 Agent Status Check
Every remember/recall/share call must verify the agent is `active`. Add to the agent resolution layer:

```python
if agent.status == "departed":
    raise HTTPException(403, f"Agent {agent.address} is departed")
```

### 7.3 Global Sharing Check
The `mnemo_share` endpoint must check `platform_config.sharing_enabled` before creating a share. The `mnemo_recall_shared` endpoint must check it before returning results.

```python
async def is_sharing_enabled() -> bool:
    row = await db.fetchrow(
        "SELECT value FROM platform_config WHERE key = 'sharing_enabled'"
    )
    return row and row["value"] == True  # JSONB true
```

---

## 8. Migration Plan

### 8.1 DB Migration

Create a single migration file (Alembic or raw SQL, match existing pattern):

1. Create `operators` table (if not exists — may need to migrate from existing structure)
2. Create `platform_config` table
3. Add `status` check constraint to `agents` if not present
4. Add `address` unique constraint to `agents` if not present
5. Seed `platform_config` with `sharing_enabled = true`
6. Create admin operator record for Tom (no Stripe IDs, status = active)

### 8.2 Existing Data

If operators/agents already exist in some form, write a one-time data migration to:
- Backfill `org` on any operators missing it
- Ensure all agents have addresses in the canonical format
- Hash any plaintext API keys

---

## 9. Testing

### 9.1 Unit Tests

- Operator CRUD (create, list, show, suspend, reinstate, rotate-key)
- Agent depart/reinstate
- Trust disable/enable toggle
- Auth: admin token works, operator key works, invalid key rejected, suspended operator rejected
- Agent status enforcement: departed agent can't remember/recall
- Sharing enforcement: global toggle off → share fails, recall_shared returns empty
- Operator suspend cascades: departing all agents
- Reinstate operator does NOT auto-reinstate agents

### 9.2 Integration Tests

- Full lifecycle: create operator → register agent → remember → recall → depart agent → recall fails → reinstate → recall works
- Suspend operator → all agents departed → reinstate operator → agents still departed → reinstate agent → works

---

## 10. Implementation Order

For Claude Code, implement in this sequence:

1. **DB migration** — create tables, constraints, seed data
2. **Auth middleware** — resolve admin token and operator API key, status checks
3. **Health endpoint** — `/health` (public) and `/health/detailed` (admin), version + schema + connectivity
4. **Operator CRUD endpoints** — POST/GET /admin/operators, suspend, reinstate, rotate-key
5. **Agent admin endpoints** — GET /admin/agents, depart, reinstate
6. **Trust admin endpoints** — status, disable, enable, list shares, revoke
7. **Enforcement points** — operator status check in auth, agent status check in remember/recall/share, global sharing check
8. **CLI** — thin wrappers over the admin endpoints
9. **Tests** — unit + integration per section 9

Estimated effort: ~1.5 days focused Claude Code work.
