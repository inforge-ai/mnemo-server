# Mnemo Sharing Scope — Implementation Spec

**Summary:** Replace the current server-level sharing boolean with a per-operator sharing scope that supports three modes: `none`, `intra` (same operator), and `full` (cross-operator). This enables the tiered product model (Individual/Team/Enterprise) while maintaining the existing GDPR-safe posture for launch.

**Effort:** ~half day. No schema migration beyond one new column + one new check.

---

## 1. Background

### Current state

Sharing is controlled by a server-level config flag (`SHARING_ENABLED=false` on mnemo-net). Three enforcement layers key off this single boolean:

1. **Postgres role permissions** — the `mnemo` DB role cannot query sharing-related tables when sharing is off
2. **API layer** — sharing endpoints return 403
3. **MCP manifest** — `mnemo_share`, `mnemo_recall_shared`, `mnemo_list_shared`, `mnemo_revoke_share` are excluded

This is all-or-nothing: every operator on the server either has sharing or doesn't.

### Target state

Per-operator sharing scope with three modes:

| Mode | Behaviour | Tier |
|------|-----------|------|
| `none` | No sharing. Share/recall_shared/list_shared/revoke_share all return 403. | Free, Individual |
| `intra` | Sharing only between agents with the same `operator_id`. Cross-operator share attempts return 403. | Team |
| `full` | Unrestricted sharing with capability controls (existing model). | Enterprise (future) |

---

## 2. Schema Changes

### 2.1 Add `sharing_scope` to operator config

The operator is identified by API key. There should already be a table that maps API keys to operators (or the API key table itself carries operator metadata). Add:

```sql
ALTER TABLE api_keys
  ADD COLUMN sharing_scope VARCHAR(5) NOT NULL DEFAULT 'none'
  CHECK (sharing_scope IN ('none', 'intra', 'full'));
```

If there's no explicit `api_keys` or `operators` table and the API key is just a config value, create a minimal operators table:

```sql
CREATE TABLE operators (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name VARCHAR(255) NOT NULL,
  api_key_hash VARCHAR(255) NOT NULL UNIQUE,
  sharing_scope VARCHAR(5) NOT NULL DEFAULT 'none'
    CHECK (sharing_scope IN ('none', 'intra', 'full')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 2.2 No changes to sharing tables

The existing tables (`capabilities`, `snapshot_atoms`, `access_log`) are unchanged. The sharing scope is an *access control check*, not a data model change. Views, capabilities, and snapshots work identically regardless of scope — the scope just determines whether a share request is allowed to proceed.

---

## 3. Enforcement Logic

### 3.1 Single enforcement function

Create one function that all sharing-related code paths call. This replaces the current server-level boolean check:

```python
from enum import Enum
from fastapi import HTTPException

class SharingScope(str, Enum):
    NONE = "none"
    INTRA = "intra"
    FULL = "full"

def check_sharing_allowed(
    operator: Operator,
    target_agent_id: UUID | None = None,
    target_operator_id: UUID | None = None,
) -> None:
    """
    Call before any sharing operation (share, recall_shared,
    list_shared, revoke_share).

    Raises HTTPException(403) if the operation is not allowed
    under the operator's sharing scope.
    """
    if operator.sharing_scope == SharingScope.NONE:
        raise HTTPException(
            status_code=403,
            detail="Sharing is not enabled for this account. "
                   "Upgrade to Team for intra-operator sharing."
        )

    if operator.sharing_scope == SharingScope.INTRA:
        # For share operations: check that target agent belongs
        # to the same operator
        if target_operator_id is not None and target_operator_id != operator.id:
            raise HTTPException(
                status_code=403,
                detail="Cross-operator sharing is not available on "
                       "this plan. Your agents can share with each "
                       "other within your account."
            )

    # SharingScope.FULL: no additional checks (existing capability
    # model handles permissions)
```

### 3.2 Where to call it

Insert `check_sharing_allowed()` at the top of each sharing endpoint, before any database work:

| Endpoint | Check needed |
|----------|-------------|
| `POST /v1/share` (mnemo_share) | `check_sharing_allowed(operator, target_operator_id=resolve_target_operator(share_with))` |
| `GET /v1/shared` (mnemo_list_shared) | `check_sharing_allowed(operator)` — just needs scope != none |
| `POST /v1/recall-shared` (mnemo_recall_shared) | `check_sharing_allowed(operator)` — just needs scope != none |
| `DELETE /v1/share/{capability_id}` (mnemo_revoke_share) | `check_sharing_allowed(operator)` — just needs scope != none |

### 3.3 Resolving the target operator

For the `intra` scope check on `mnemo_share`, you need to know which operator the target agent belongs to. The agent address is `{agent_name}:{username}.{org}`. You need a lookup:

```python
async def resolve_target_operator(share_with: str) -> UUID:
    """
    Given an agent address like 'coding-agent:tom.acme',
    return the operator_id that owns that agent.
    """
    agent = await agent_repo.get_by_address(share_with)
    if agent is None:
        raise HTTPException(404, f"Agent '{share_with}' not found")
    return agent.operator_id
```

If the agent model doesn't currently carry an `operator_id`, it needs one. Every agent is registered under an API key, and the API key maps to an operator. This relationship should already exist implicitly — make it explicit with a foreign key if it isn't already.

---

## 4. MCP Manifest Changes

### Current behaviour

When `SHARING_ENABLED=false`, sharing tools are excluded from the MCP manifest entirely. Agents don't even see them.

### New behaviour

The MCP manifest is generated per-connection (since each connection authenticates with an API key that maps to an operator). The manifest includes sharing tools based on the operator's scope:

```python
def get_mcp_tools(operator: Operator) -> list[Tool]:
    tools = [
        mnemo_remember,
        mnemo_recall,
        mnemo_stats,
    ]

    if operator.sharing_scope in (SharingScope.INTRA, SharingScope.FULL):
        tools.extend([
            mnemo_share,
            mnemo_recall_shared,
            mnemo_list_shared,
            mnemo_revoke_share,
        ])

    return tools
```

This means:
- Free/Individual operators see 3 tools (remember, recall, stats)
- Team operators see 7 tools (+ share, recall_shared, list_shared, revoke_share)
- Enterprise operators see the same 7 (sharing tools behave identically, just without the cross-operator restriction)

### Tool description update

For the `intra` scope, update the `mnemo_share` tool description dynamically to make the boundary clear to the agent:

```python
if operator.sharing_scope == SharingScope.INTRA:
    mnemo_share.description = (
        "Share memories with another agent in your account. "
        "The target agent must belong to the same operator. "
        "Creates a named view that the target agent can query."
    )
```

This helps the LLM avoid attempting cross-operator shares that will 403.

---

## 5. API Layer Changes

### Remove server-level boolean

Delete (or deprecate) the `SHARING_ENABLED` environment variable. The three-layer enforcement is replaced by:

1. **Operator-level scope** (this spec) — checked in the API layer via `check_sharing_allowed()`
2. **Capabilities table** — existing `revoked`, `expires_at` checks remain unchanged
3. **MCP manifest** — conditionally includes sharing tools per operator scope

The Postgres role-level permissions can stay as a defence-in-depth measure, but they're no longer the primary enforcement mechanism.

### Error messages

Return actionable 403 messages that tell the operator what to do:

| Scope | Attempted operation | Response |
|-------|-------------------|----------|
| `none` | Any sharing operation | `403: Sharing is not enabled for this account. Upgrade to Team for intra-operator sharing.` |
| `intra` | Share with agent owned by different operator | `403: Cross-operator sharing is not available on this plan. Your agents can share with each other within your account.` |
| `intra` | Share with agent owned by same operator | Proceed normally |
| `full` | Any sharing operation | Proceed normally |

---

## 6. Admin Interface

### Setting the scope

For the paid beta, scope is set manually (admin CLI or direct DB update):

```bash
# Promote an operator to Team (intra sharing)
UPDATE operators SET sharing_scope = 'intra' WHERE name = 'acme-corp';

# Future: promote to Enterprise (full sharing)
UPDATE operators SET sharing_scope = 'full' WHERE name = 'big-enterprise';
```

When Stripe billing is wired up, the scope is set automatically based on the subscription tier.

### Admin API (optional, can defer)

```
PATCH /admin/v1/operators/{operator_id}
{
  "sharing_scope": "intra"
}
```

---

## 7. Migration Path

### For mnemo-net (customer-facing production)

1. Add `sharing_scope` column, default `none`
2. Remove `SHARING_ENABLED` env var (or ignore it — the per-operator scope takes precedence)
3. Deploy. All existing operators default to `none` — behaviour is unchanged from current "sharing disabled" state
4. When a Team customer signs up, set their `sharing_scope = 'intra'`

### For inforge-ops (internal Mnemo)

Set Inforge's own operator to `sharing_scope = 'full'` — your internal agents (ABACAB, Clio, CFO agent, etc.) continue to share freely across all agents as they do today.

---

## 8. Testing

### Unit tests

```
test_sharing_none_returns_403_on_share
test_sharing_none_returns_403_on_recall_shared
test_sharing_none_returns_403_on_list_shared
test_sharing_none_returns_403_on_revoke_share
test_sharing_none_excludes_tools_from_manifest

test_sharing_intra_allows_same_operator_share
test_sharing_intra_blocks_cross_operator_share
test_sharing_intra_allows_recall_shared
test_sharing_intra_includes_tools_in_manifest

test_sharing_full_allows_cross_operator_share
test_sharing_full_allows_all_operations
```

### Integration tests

```
test_intra_share_flow:
  1. Create operator A with scope=intra
  2. Register agent-1 and agent-2 under operator A
  3. agent-1 shares with agent-2 → 200
  4. agent-2 recall_shared → returns atoms
  5. agent-1 revokes → 200
  6. agent-2 recall_shared → empty

test_intra_cross_operator_blocked:
  1. Create operator A (scope=intra) and operator B (scope=intra)
  2. Register agent-1 under A, agent-3 under B
  3. agent-1 shares with agent-3 → 403

test_scope_upgrade:
  1. Create operator A with scope=none
  2. Attempt share → 403
  3. UPDATE scope to 'intra'
  4. Attempt same-operator share → 200
```

---

## 9. GDPR Notes

### `none` scope (Free/Individual)

No sharing = no cross-controller data flow. Standard data processor position. Identical legal posture to current production.

### `intra` scope (Team)

All sharing stays within a single operator's agents. One data controller's data, processed by Inforge as data processor. No additional GDPR complexity beyond the standard DPA.

**Confirm with Blick Rothenberg:** that intra-operator sharing (same API key, same controller) does not constitute a new processing activity requiring additional disclosure or consent.

### `full` scope (Enterprise — deferred)

Cross-operator sharing creates data flows between controllers. Requires: DPA amendments, DPIA, explicit consent framework, and potentially data sharing agreements between operators. **Do not enable on mnemo-net until legal infrastructure is in place.**

---

## 10. Deferred Work

- **Stripe integration:** auto-set `sharing_scope` based on subscription tier
- **Recipient-side mute/hide:** per earlier spec discussion, defer to post-beta
- **Agent departure policy:** what happens to shared views when the sharing agent is deregistered — default to "atoms become inaccessible", formalise later
- **Scope downgrade handling:** if a Team operator downgrades to Individual, existing shares should be revoked or frozen. Define the behaviour before enabling self-service tier changes.
