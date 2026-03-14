# Mnemo Shared View Revocation — Design Spec

**Version:** 0.1 (design phase)
**Date:** 2026-03-14
**Status:** Draft

---

## Context

Mnemo's sharing model creates **snapshot views** — a frozen set of atom IDs (`snapshot_atoms`) with a **capability** granting a target agent read access. At query time, `recall_shared` reads the grantor's atoms live through the snapshot scope. The `capabilities` table already has `revoked BOOLEAN DEFAULT false` and `expires_at TIMESTAMPTZ`, and `recall_all_shared` already checks both.

**What's missing:** There is no way for a grantor to set `revoked = true` — no REST endpoint, no MCP tool.

## Scope

This spec covers **grantor-initiated revocation of a specific shared view**. It does not cover:

- Recipient-side hide/mute (deferred — see Future Work)
- Bulk revocation (revoke all shares with a given agent)
- Atom lifecycle when a sharing agent leaves the system (existing open decision, default: atoms become inaccessible)

## Semantics

**What revoke does:**

- Sets `capabilities.revoked = true` and records `revoked_at` timestamp
- All subsequent `recall_shared` and `recall_all_shared` queries against this capability return nothing (the `WHERE c.revoked = false` clause already handles this)
- The view record and `snapshot_atoms` rows are **preserved** (soft delete for audit trail)
- The underlying atoms in the grantor's graph are **untouched**

**What revoke does NOT do:**

- It does not claw back information the recipient agent has already consumed in past conversations. The recipient's LLM may have already reasoned over those atoms. Revocation controls future access, not past exposure.
- It does not delete the view or its atom associations
- It does not notify the recipient agent (v0.2 — no event system yet)

**Idempotency:** Revoking an already-revoked capability is a no-op (returns success with current state).

## Database Changes

### `capabilities` table — add column

```sql
ALTER TABLE capabilities
ADD COLUMN revoked_at TIMESTAMPTZ DEFAULT NULL;
```

The existing `revoked` boolean remains the query-time gate. `revoked_at` provides audit context. Both are set together on revocation.

No other schema changes required — `snapshot_atoms`, `views`, and `access_log` are unchanged.

## REST API

### `POST /v1/agents/{agent_id}/capabilities/{capability_id}/revoke`

**Auth:** Operator API key (existing auth model). The server validates that the authenticated operator owns the agent identified by `agent_id`, and that `agent_id` is the `grantor_id` on the capability.

**Request body:** None (empty or `{}`).

**Response `200 OK`:**

```json
{
  "capability_id": "uuid",
  "view_id": "uuid",
  "grantee_id": "uuid",
  "revoked": true,
  "revoked_at": "2026-03-14T12:00:00Z",
  "was_already_revoked": false
}
```

**Error cases:**

| Status | Condition |
|--------|-----------|
| `404`  | Capability not found, or `agent_id` is not the grantor |
| `403`  | Operator does not own `agent_id` |

**Implementation (view_service.py):**

```python
async def revoke_shared_view(
    conn: asyncpg.Connection,
    grantor_id: UUID,
    capability_id: UUID,
) -> dict:
    """Revoke a shared view capability. Idempotent."""
    row = await conn.fetchrow(
        """
        SELECT id, view_id, grantee_id, revoked, revoked_at
        FROM capabilities
        WHERE id = $1 AND grantor_id = $2
        """,
        capability_id,
        grantor_id,
    )
    if not row:
        return None  # caller returns 404

    was_already_revoked = row["revoked"]

    if not was_already_revoked:
        await conn.execute(
            """
            UPDATE capabilities
            SET revoked = true, revoked_at = now()
            WHERE id = $1
            """,
            capability_id,
        )

    # Audit log
    await conn.execute(
        """
        INSERT INTO access_log (agent_id, action, target_id, metadata)
        VALUES ($1, 'revoke_shared', $2, $3)
        """,
        grantor_id,
        row["view_id"],
        json.dumps({
            "capability_id": str(capability_id),
            "grantee_id": str(row["grantee_id"]),
            "was_already_revoked": was_already_revoked,
        }),
    )

    updated = await conn.fetchrow(
        "SELECT revoked_at FROM capabilities WHERE id = $1",
        capability_id,
    )

    return {
        "capability_id": capability_id,
        "view_id": row["view_id"],
        "grantee_id": row["grantee_id"],
        "revoked": True,
        "revoked_at": updated["revoked_at"],
        "was_already_revoked": was_already_revoked,
    }
```

## MCP Tool

### `mnemo_revoke_share`

**Description:** Revoke a previously shared memory view, removing the recipient agent's ability to query it.

**Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `capability_id` | `string` | Yes | The capability ID to revoke (from `mnemo_list_shared` or the original `mnemo_share` response) |
| `agent_id` | `string` | No | Override agent ID (defaults to configured agent) |

**MCP tool definition:**

```python
@server.tool()
async def mnemo_revoke_share(
    capability_id: str,
    agent_id: str | None = None,
) -> str:
    """Revoke a previously shared memory view.

    Removes the recipient agent's ability to query this shared view.
    The underlying memories are not affected. This action is immediate
    but does not claw back information already consumed.

    Args:
        capability_id: The capability ID to revoke. Get this from
            mnemo_list_shared (outbound shares) or the original
            mnemo_share response.
        agent_id: Optional agent ID override.
    """
```

**Return format (success):**

```
Shared view revoked.
- View: {view_name}
- Recipient: {grantee_address}
- Revoked at: {timestamp}
```

**Return format (already revoked):**

```
This share was already revoked on {revoked_at}.
```

**Return format (not found):**

```
Capability not found, or you are not the grantor of this share.
```

## Discovery: How the Agent Finds the `capability_id`

The current `mnemo_list_shared` tool shows views shared **with** this agent (inbound). To support revocation, we also need to list views shared **by** this agent (outbound).

### Option: Extend `mnemo_list_shared` with a `direction` parameter

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `direction` | `string` | `"inbound"` | `"inbound"` (shared with me) or `"outbound"` (shared by me) |

**Outbound response includes `capability_id`** so it can be passed directly to `mnemo_revoke_share`:

```
Outbound shares:

- 'nels-shared-memories' → nels-claude-desktop:nels.inforge
  Capability: {capability_id}
  Atoms: 20 | Granted: 2026-03-12
  Status: active
```

### REST endpoint for outbound listing

```
GET /v1/agents/{agent_id}/capabilities?direction=outbound
```

Returns capabilities where `grantor_id = agent_id`, including `capability_id`, `view_id`, `grantee_id`, `revoked`, `revoked_at`, and view metadata.

## Audit Trail

All revocations are logged to `access_log` with:

- `action = 'revoke_shared'`
- `target_id = view_id`
- `metadata` includes `capability_id`, `grantee_id`, and `was_already_revoked`

This complements the existing `recall_shared` audit entries, giving a complete timeline of share → access → revoke.

## Testing Plan

1. **Happy path:** Share a view → verify recall works → revoke → verify recall returns empty
2. **Idempotency:** Revoke twice → second call returns `was_already_revoked: true`
3. **Auth boundary:** Agent B tries to revoke Agent A's capability → 404
4. **Expired + revoked:** Capability with `expires_at` in the past AND `revoked = true` → still shows as revoked (not expired)
5. **Outbound listing:** After sharing 2 views, `mnemo_list_shared(direction="outbound")` returns both with capability IDs

## Future Work

- **Recipient-side mute:** Allow the recipient to hide/mute a shared view from their recall space without affecting the grantor's state. Addresses the noise/relevance use case (unwanted views cluttering `recall_shared` results). Separate from revocation — muted views can be unmuted, and the grantor has no visibility into mute state.
- **Bulk revocation:** `POST /v1/agents/{agent_id}/capabilities/revoke-all?grantee_id={id}` — revoke all active capabilities granted to a specific agent.
- **Agent departure:** When a sharing agent is removed from the system, default behavior is that shared views become inaccessible (atoms gone → recall returns nothing). Open decision on whether to explicitly revoke all outbound capabilities on agent deletion vs. let them fail naturally.
- **Revocation events/webhooks:** Notify the recipient agent that a share has been revoked. Requires an event system that doesn't exist yet.
