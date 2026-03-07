# Mnemo — Build Spec: Views, Sharing, and Client Production Fixes

## For: Claude Code
## Date: 6 March 2026
## Status: READY TO BUILD
## Estimated time: 5–7 hours focused work

---

## Context

The Mnemo server already has working `remember` and `recall` endpoints, a
running MCP connection, and a Python client in use by Clio. This spec covers
the three remaining feature areas needed before the public GitHub launch:

1. **View creation + skill export** — the agent-to-agent differentiator
2. **Capabilities: grant + recall_shared** — closes the sharing loop
3. **Client production fixes** — hardens `mnemo_client.py` for public release

Do them in this order. Each section is self-contained and testable before
moving to the next.

---

## Part 1: View Creation and Skill Export

### What to build

Two new server endpoints and one new service. The client methods
`create_view()`, `list_views()`, and `export_skill()` already exist and are
correct — the server just needs to back them.

### 1.1 `POST /v1/agents/{agent_id}/views`

**File:** `server/routes/views.py`

**Request model** (already defined in `server/models.py`):
```python
class ViewCreate(BaseModel):
    name: str
    description: Optional[str] = None
    atom_filter: dict   # {"atom_types": [...], "domain_tags": [...]}
```

**Response model** (already defined):
```python
class ViewResponse(BaseModel):
    id: UUID
    owner_agent_id: UUID
    name: str
    description: Optional[str]
    alpha: float          # always 1.0 for v0.1
    atom_filter: dict
    atom_count: int       # number of atoms captured in this snapshot
    created_at: datetime
```

**Implementation — view_service.py CREATE SNAPSHOT FLOW:**

```python
async def create_view(agent_id: UUID, req: ViewCreate, pool) -> ViewResponse:
    """
    1. Validate agent exists and is active.
    2. Parse atom_filter: extract atom_types (list|None) and
       domain_tags (list|None). Unknown keys are ignored.
    3. Query matching atoms at this moment:

       SELECT id FROM atoms
       WHERE agent_id = $agent_id
         AND is_active = true
         AND ($atom_types IS NULL OR atom_type = ANY($atom_types))
         AND ($domain_tags IS NULL OR domain_tags && $domain_tags)

    4. INSERT into views:
       (owner_agent_id, name, description, alpha=1.0,
        atom_filter, snapshot_at=now(), created_at=now())

    5. Bulk INSERT into snapshot_atoms:
       INSERT INTO snapshot_atoms (view_id, atom_id)
       VALUES ($view_id, $atom_id), ...   -- one row per matched atom

    6. Return ViewResponse with atom_count = len(matched atoms).

    The snapshot is now immutable. Source atom decay does not affect it.
    """
```

**Validation rules:**
- Agent must exist and `is_active = true`. Return 404 if not.
- `atom_filter` must be a dict. Return 422 if missing or wrong type.
- If the filter matches 0 atoms, still create the view (empty snapshot
  is valid — it's a legitimate state). Return atom_count=0.
- `alpha` is always 1.0 in v0.1. Hard-code it. Do not accept it from
  the request.

---

### 1.2 `GET /v1/agents/{agent_id}/views`

Simple list query:

```sql
SELECT v.*, COUNT(sa.atom_id) as atom_count
FROM views v
LEFT JOIN snapshot_atoms sa ON sa.view_id = v.id
WHERE v.owner_agent_id = $agent_id
GROUP BY v.id
ORDER BY v.created_at DESC
```

Returns `list[ViewResponse]`.

---

### 1.3 `GET /v1/agents/{agent_id}/views/{view_id}/export_skill`

**Response model** (already defined):
```python
class SkillExport(BaseModel):
    view_id: UUID
    name: str
    description: Optional[str]
    domain_tags: list[str]        # union of all atom domain_tags in snapshot
    procedures: list[AtomResponse]
    supporting_facts: list[AtomResponse]
    metadata: dict
    rendered_markdown: str        # the actual SKILL.md content
```

**Implementation — view_service.py EXPORT SKILL FLOW:**

```python
async def export_skill(agent_id: UUID, view_id: UUID, pool) -> SkillExport:
    """
    1. Validate view exists and owner_agent_id == agent_id. Return 404/403.

    2. Load ALL atoms from snapshot:
       SELECT a.* FROM atoms a
       JOIN snapshot_atoms sa ON sa.atom_id = a.id
       WHERE sa.view_id = $view_id

       Include inactive atoms (snapshot freezes them at creation time —
       even if source atom has since decayed, the snapshot retains it).
       Use confidence_expected (α/(α+β)), not confidence_effective, for
       the export — the receiving agent gets the knowledge at its original
       confidence and applies its own decay from import time.

    3. Split atoms:
       procedures = [a for a in atoms if a.atom_type == 'procedural']
       supporting  = [a for a in atoms if a.atom_type == 'semantic']
       (episodic and relational atoms are excluded from skill exports —
        they're experiential context, not transferable knowledge)

    4. Collect domain_tags: sorted union of all atom domain_tags.

    5. Render markdown (see template below).

    6. Return SkillExport.
    """
```

**Markdown rendering template:**

```python
def render_skill_markdown(view, procedures, supporting_facts, agent_name, timestamp) -> str:
    lines = []
    lines.append(f"# {view['name']}")
    lines.append("")
    if view.get('description'):
        lines.append(view['description'])
        lines.append("")
    lines.append(f"*Generated by Mnemo on {timestamp}*")
    lines.append(f"*Source agent: {agent_name}*")
    lines.append(f"*Domain: {', '.join(domain_tags)}*")
    lines.append("")
    lines.append("---")
    lines.append("")

    if procedures:
        lines.append("## Procedures")
        lines.append("")
        for p in procedures:
            conf_pct = int(p['confidence_expected'] * 100)
            lines.append(f"### {p['text_content']}")
            lines.append(f"*Confidence: {conf_pct}%*")
            lines.append("")
            # If structured has a 'code' key, render it as a code block
            if p.get('structured', {}).get('code'):
                lines.append("```")
                lines.append(p['structured']['code'])
                lines.append("```")
                lines.append("")
            # Find supporting facts linked to this procedure (from edges)
            linked = [s for s in supporting_facts
                      if is_linked(p['id'], s['id'])]  # check edges table
            if linked:
                lines.append("**Supporting knowledge:**")
                for s in linked:
                    conf_pct = int(s['confidence_expected'] * 100)
                    lines.append(f"- {s['text_content']} ({conf_pct}%)")
                lines.append("")
            lines.append("---")
            lines.append("")

    if supporting_facts:
        lines.append("## Background Knowledge")
        lines.append("")
        for s in supporting_facts:
            conf_pct = int(s['confidence_expected'] * 100)
            lines.append(f"- {s['text_content']} ({conf_pct}%)")
        lines.append("")

    return "\n".join(lines)
```

**Edge case:** if the snapshot has no procedural atoms (e.g. the filter only
captured semantic atoms), still return a valid SkillExport — procedures=[] and
render just the Background Knowledge section. Not an error.

---

### 1.4 Tests for Part 1

File: `tests/test_views.py`

```
test_create_view_happy_path
  - register agent, remember 3 items with domain_tags=["python"]
  - create view with filter {"domain_tags": ["python"]}
  - assert atom_count == 3
  - assert view_id is UUID

test_create_view_empty_snapshot
  - create view with filter that matches nothing
  - assert atom_count == 0, no error

test_snapshot_is_immutable
  - create view, note atom_count
  - soft-delete one source atom (DELETE /v1/agents/{id}/atoms/{atom_id})
  - export_skill still returns same atom_count
  (snapshot_atoms join includes atoms regardless of is_active)

test_export_skill_markdown
  - create agent with procedural and semantic memories
  - create view, export_skill
  - assert rendered_markdown contains "## Procedures"
  - assert rendered_markdown contains the procedural atom text

test_export_skill_no_procedures
  - create view with only semantic atoms
  - assert procedures == [], no error, rendered_markdown has "## Background Knowledge"

test_wrong_agent_cannot_export
  - agent A creates view
  - agent B tries to export it
  - assert 403
```

---

## Part 2: Capabilities — Grant and Recall Through Shared View

### What to build

Three new endpoints. The client methods `grant()`, `revoke()`,
`list_shared_views()`, and `recall_shared()` already exist and are correct.

---

### 2.1 `POST /v1/agents/{agent_id}/grant`

**File:** `server/routes/capabilities.py`

**Request model:**
```python
class GrantCreate(BaseModel):
    view_id: UUID
    grantee_id: UUID
    permissions: list[str] = ["read"]
    expires_at: Optional[datetime] = None
```

**Response:** `CapabilityResponse`

**Implementation:**

```python
async def grant(agent_id: UUID, req: GrantCreate, pool) -> CapabilityResponse:
    """
    1. Validate grantor (agent_id) exists and is active.
    2. Validate view exists and view.owner_agent_id == agent_id.
       Return 403 if agent doesn't own the view.
    3. Validate grantee (req.grantee_id) exists and is active.
    4. Check for existing non-revoked capability for same
       (view_id, grantee_id) pair. If found, return it (idempotent).
    5. INSERT into capabilities:
       (view_id, grantor_id=agent_id, grantee_id, permissions,
        revoked=false, parent_cap_id=NULL, expires_at, created_at=now())
    6. Log in access_log: {action: 'grant', target_id: capability.id}
    7. Return CapabilityResponse.
    """
```

**Validation rules:**
- Only the view owner can grant. 403 if agent_id != view.owner_agent_id.
- `permissions` must be a subset of `["read"]` for v0.1. Other values
  accepted but ignored (future-proof). Return the stored value as-is.
- `expires_at` if provided must be in the future. Return 422 if in the past.

---

### 2.2 `POST /v1/capabilities/{capability_id}/revoke`

```python
async def revoke(capability_id: UUID, pool) -> dict:
    """
    1. Look up capability. Return 404 if not found.
    2. If already revoked, return {revoked: true, message: "already revoked"}.
    3. Use the revoke_agent_capabilities SQL function pattern but
       for a single capability and its descendants:

       WITH RECURSIVE cap_tree AS (
           SELECT id FROM capabilities WHERE id = $capability_id
           UNION
           SELECT c.id FROM capabilities c
           JOIN cap_tree ct ON c.parent_cap_id = ct.id
           WHERE c.revoked = false
       )
       UPDATE capabilities SET revoked = true
       WHERE id IN (SELECT id FROM cap_tree)

    4. Log in access_log: {action: 'revoke', target_id: capability_id}
    5. Return {revoked: true, capabilities_revoked: N}
    """
```

Note: this endpoint does not require `agent_id` in the path — the capability
ID is sufficient. Future auth layer will validate the caller owns the
grantor agent. For v0.1, no auth, so any caller can revoke any capability.
Document this as a known v0.1 limitation.

---

### 2.3 `GET /v1/agents/{agent_id}/shared_views`

Lists views that have been shared *with* this agent (i.e. they are a grantee):

```sql
SELECT v.*, COUNT(sa.atom_id) as atom_count,
       c.id as capability_id, c.permissions, c.expires_at
FROM capabilities c
JOIN views v ON v.id = c.view_id
LEFT JOIN snapshot_atoms sa ON sa.view_id = v.id
WHERE c.grantee_id = $agent_id
  AND c.revoked = false
  AND (c.expires_at IS NULL OR c.expires_at > now())
GROUP BY v.id, c.id
ORDER BY c.created_at DESC
```

Returns `list[ViewResponse]` (add `capability_id` to the response for
the grantee — they need it to recall through the view).

---

### 2.4 `POST /v1/agents/{agent_id}/shared_views/{view_id}/recall`

This is the critical endpoint — recall through a shared view, scope-bounded.

**Request:**
```python
class SharedRecallRequest(BaseModel):
    query: str
    min_confidence: float = 0.1
    max_results: int = 10
    expand_graph: bool = True
    expansion_depth: int = 2
```

**Implementation:**

```python
async def recall_shared(agent_id: UUID, view_id: UUID,
                         req: SharedRecallRequest, pool) -> RetrieveResponse:
    """
    1. Validate that agent_id has a non-revoked, non-expired capability
       on view_id:

       SELECT c.id FROM capabilities c
       WHERE c.grantee_id = $agent_id
         AND c.view_id = $view_id
         AND c.revoked = false
         AND (c.expires_at IS NULL OR c.expires_at > now())
       LIMIT 1

       Return 403 if no valid capability found.

    2. Load the view's atom_filter (for scope boundary).

    3. Retrieve ONLY from snapshot_atoms for this view:

       SELECT a.*,
              1 - (a.embedding <=> $query_embedding) as similarity,
              effective_confidence(
                  a.confidence_alpha, a.confidence_beta,
                  a.decay_type, a.decay_half_life_days,
                  a.created_at, a.last_accessed, a.access_count
              ) as eff_conf
       FROM atoms a
       JOIN snapshot_atoms sa ON sa.atom_id = a.id
       WHERE sa.view_id = $view_id
         AND ($atom_types IS NULL OR a.atom_type = ANY($atom_types))
       ORDER BY similarity DESC
       LIMIT $max_results * 2

       Then filter: WHERE eff_conf >= $min_confidence
       Then limit to $max_results.

    4. If expand_graph=True: graph expansion is SCOPE-BOUNDED to
       snapshot_atoms only. No edge can pull in an atom outside
       the snapshot. Use the scope-bounded graph expansion pattern:

       The scope_filter for expansion = the view's atom_filter INTERSECTED
       with the constraint that the expanded atom must also appear in
       snapshot_atoms for this view.

       In practice: join expansion candidates against snapshot_atoms
       WHERE snapshot_atoms.view_id = $view_id. This is the safety
       guarantee — expansion cannot leak atoms outside the snapshot.

    5. Update access_count on the capability:
       UPDATE capabilities SET access_count = access_count + 1
       (add access_count INTEGER DEFAULT 0 to capabilities table if not present)

    6. Log in access_log: {action: 'recall_shared', target_id: view_id,
       metadata: {grantee_id: agent_id, results_returned: N}}

    7. Return RetrieveResponse (same shape as normal recall —
       the grantee doesn't need to know it came from a shared view).
    """
```

**Critical invariant:** An atom returned by `recall_shared` must always
appear in `snapshot_atoms` for the given `view_id`. If this invariant is
ever violated, the scope boundary has been breached. Add an assertion in
the test suite to verify this.

---

### 2.5 Tests for Part 2

File: `tests/test_capabilities.py`

```
test_grant_happy_path
  - register agent_a and agent_b
  - agent_a remembers 3 procedural memories tagged ["python"]
  - agent_a creates view, grants to agent_b
  - assert CapabilityResponse returned with revoked=false

test_grant_idempotent
  - grant same view to same agent twice
  - assert same capability_id returned, no duplicate rows

test_only_owner_can_grant
  - agent_b tries to grant agent_a's view
  - assert 403

test_recall_shared_happy_path
  - agent_a has memories, creates view, grants to agent_b
  - agent_b calls recall_shared with relevant query
  - assert atoms returned
  - assert all returned atom ids appear in snapshot_atoms for this view
    (the critical scope invariant)

test_recall_shared_scope_boundary
  - agent_a has memories tagged ["python"] and ["finance"]
  - view filter is {"domain_tags": ["python"]}
  - agent_b recall_shared with a finance-related query
  - assert NO finance-tagged atoms in results (even via graph expansion)

test_recall_shared_revoked_capability
  - grant, then revoke the capability
  - agent_b tries recall_shared
  - assert 403

test_recall_shared_expired_capability
  - grant with expires_at = 1 second in the future
  - sleep(2)
  - agent_b tries recall_shared
  - assert 403

test_revoke_cascades
  - agent_a grants view to agent_b (cap_1)
  - agent_b re-grants same view to agent_c (cap_2, parent_cap_id=cap_1)
    (note: re-granting is not exposed in v0.1 client but test at DB level)
  - revoke cap_1
  - assert cap_2 is also revoked

test_list_shared_views
  - agent_b has two capabilities granted by different agents
  - list_shared_views returns both
  - revoke one
  - list_shared_views returns one
```

---

## Part 3: Client Production Fixes

**File:** `mnemo_client.py` (currently in Clio project)

These are all mechanical changes. Do them in a single pass. Each is
independent.

---

### 3.1 Make `api_key` required

```python
# Before
def __init__(self, base_url: str = "http://localhost:8000",
             api_key: Optional[str] = None):

# After
def __init__(self, base_url: str = "https://api.mnemo.ai", api_key: str):
    if not api_key:
        raise ValueError(
            "api_key is required. Get yours at https://mnemo.ai"
        )
```

Fail immediately at construction, not at first HTTP call.

---

### 3.2 UUID normalisation

Add this helper at the top of the class (or as a module-level function):

```python
def _uid(v) -> str:
    """Normalise UUID or string to plain string for HTTP."""
    return str(v)
```

Apply `_uid()` to every UUID argument before it appears in an f-string URL
or JSON body. Audit every method. The inconsistency is currently:
- `link()` and `grant()` already call `str()` — make these use `_uid()`
- `remember()`, `recall()`, `store_atom()`, `get_atom()`, `delete_atom()`,
  `get_agent()`, `stats()`, `depart()`, `create_view()`, `list_views()`,
  `export_skill()`, `list_shared_views()`, `recall_shared()` — add `_uid()`

---

### 3.3 Exception hierarchy

Add before the class definition:

```python
class MnemoError(Exception):
    """Base exception for all Mnemo client errors."""
    pass

class MnemoAuthError(MnemoError):
    """Raised on 401 or 403 responses."""
    pass

class MnemoNotFoundError(MnemoError):
    """Raised on 404 responses."""
    pass

class MnemoServerError(MnemoError):
    """Raised on 5xx responses."""
    pass
```

Add this method to `MnemoClient`:

```python
def _raise_for_status(self, resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise MnemoAuthError("Invalid or missing API key")
    if resp.status_code == 403:
        raise MnemoAuthError(f"Permission denied: {resp.text}")
    if resp.status_code == 404:
        raise MnemoNotFoundError(resp.text)
    if resp.status_code >= 500:
        raise MnemoServerError(
            f"Mnemo server error {resp.status_code}: {resp.text}"
        )
    resp.raise_for_status()
```

Replace every `resp.raise_for_status()` call with `self._raise_for_status(resp)`.

---

### 3.4 TypedDicts for primary interface

Add after the exception classes:

```python
from typing import TypedDict

class RememberResult(TypedDict):
    atoms_created: int
    edges_created: int
    duplicates_merged: int

class Atom(TypedDict):
    id: str
    atom_type: str
    text_content: str
    confidence_expected: float
    confidence_effective: float
    domain_tags: list[str]
    source_type: str
    created_at: str
    access_count: int

class RecallResult(TypedDict):
    atoms: list[Atom]
    expanded_atoms: list[Atom]
    total_retrieved: int

class AgentStats(TypedDict):
    agent_id: str
    total_atoms: int
    active_atoms: int
    atoms_by_type: dict
    total_edges: int
    avg_effective_confidence: float
    active_views: int
    granted_capabilities: int
    received_capabilities: int
```

Update return type annotations on `remember()`, `recall()`, and `stats()`.
Leave all other methods returning `dict` for now.

---

### 3.5 `MnemoClientSync` wrapper

Add after `MnemoClient`:

```python
import asyncio
import concurrent.futures

class MnemoClientSync:
    """
    Synchronous wrapper around MnemoClient.

    Use this in non-async contexts (e.g. inside a synchronous agent loop,
    a WebSocket handler, or a script). Handles the case where an event
    loop is already running by offloading to a worker thread.

    Example:
        client = MnemoClientSync(api_key="your-key", agent_id="your-uuid")
        client.remember("pandas.read_csv coerces mixed types")
        results = client.recall("loading CSV files")
    """

    def __init__(self, api_key: str,
                 agent_id: str,
                 base_url: str = "https://api.mnemo.ai"):
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._agent_id = agent_id
        self._base_url = base_url

    def _run(self, coro):
        try:
            asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        except RuntimeError:
            return asyncio.run(coro)

    def remember(self, text: str,
                 domain_tags: list[str] | None = None) -> RememberResult:
        async def _():
            async with MnemoClient(self._base_url, self._api_key) as c:
                return await c.remember(self._agent_id, text, domain_tags)
        return self._run(_())

    def recall(self, query: str, **kwargs) -> RecallResult:
        async def _():
            async with MnemoClient(self._base_url, self._api_key) as c:
                return await c.recall(self._agent_id, query, **kwargs)
        return self._run(_())

    def stats(self) -> AgentStats:
        async def _():
            async with MnemoClient(self._base_url, self._api_key) as c:
                return await c.stats(self._agent_id)
        return self._run(_())
```

Note: `MnemoClientSync` takes `agent_id` at construction (not per-call)
because sync usage is typically single-agent. The async `MnemoClient`
keeps `agent_id` per-call because it may be used by an operator managing
multiple agents.

---

### 3.6 Short-query guard in `MnemoTools.recall_for_context`

This is in `mnemo_tools.py` in the Clio project, not in `mnemo_client.py`.
Small change, worth making now:

```python
def recall_for_context(self, query: str) -> str:
    # Don't bother recalling for very short queries — results will be noise
    if len(query.strip()) < 10:
        return ""
    try:
        ...
```

---

### 3.7 `domain_tags` in `MnemoTools.remember_conversation`

Also in `mnemo_tools.py`. Add domain tags so Clio's memories are
filterable:

```python
def remember_conversation(self, user_message: str, response: str) -> None:
    try:
        self.execute(
            "mnemo_remember",
            {
                "text": f"User: {user_message}\nClio: {response}",
                "domain_tags": ["clio", "conversation"],   # ← add this
            },
        )
    except Exception as e:
        logger.debug("Mnemo remember_conversation failed (non-fatal): %s", e)
```

---

### 3.8 Tests for Part 3

File: `tests/test_client.py`  
Use `respx` to mock httpx at the transport layer.

```
test_api_key_required
  - MnemoClient("http://localhost", api_key="") raises ValueError
  - MnemoClient("http://localhost", api_key=None) raises ValueError

test_auth_header_on_every_request
  - mock any endpoint, assert request has Authorization: Bearer test-key

test_remember_returns_typed_dict
  - mock POST /v1/agents/{id}/remember → 200
  - assert result is RememberResult with correct keys

test_recall_returns_typed_dict
  - mock POST /v1/agents/{id}/recall → 200
  - assert result is RecallResult with atoms list

test_401_raises_mnemo_auth_error
  - mock any endpoint → 401
  - assert raises MnemoAuthError

test_403_raises_mnemo_auth_error
  - mock any endpoint → 403
  - assert raises MnemoAuthError

test_404_raises_mnemo_not_found_error
  - mock any endpoint → 404
  - assert raises MnemoNotFoundError

test_500_raises_mnemo_server_error
  - mock any endpoint → 500
  - assert raises MnemoServerError

test_uuid_object_accepted
  - pass UUID object as agent_id to remember()
  - assert no TypeError, request URL contains string form

test_sync_client_remember
  - use MnemoClientSync in a plain synchronous context
  - assert remember() returns RememberResult without event loop error

test_sync_client_in_running_loop
  - call MnemoClientSync.remember() from within asyncio.run()
  - assert it completes without "event loop already running" error
```

---

## Sequence Summary

| Step | What | Where | Time |
|------|------|-------|------|
| 1 | `server/routes/views.py` + `view_service.py` | Private server repo | 2h |
| 2 | `server/routes/capabilities.py` | Private server repo | 1.5h |
| 3 | `tests/test_views.py` + `tests/test_capabilities.py` | Private server repo | 1h |
| 4 | Client fixes (3.1–3.5) in `mnemo_client.py` | Clio project | 1h |
| 5 | `mnemo_tools.py` fixes (3.6–3.7) | Clio project | 15min |
| 6 | `tests/test_client.py` | Clio project | 30min |

**End-of-day goal:** run this sequence for real and have it work:

```python
# Agent A accumulates procedural knowledge
view = await client.create_view(
    agent_id=agent_a,
    name="pandas-skills",
    atom_filter={"atom_types": ["procedural"], "domain_tags": ["python"]},
)
skill = await client.export_skill(agent_id=agent_a, view_id=view["id"])
print(skill["rendered_markdown"])   # ← readable SKILL.md output

cap = await client.grant(
    agent_id=agent_a,
    view_id=view["id"],
    grantee_id=agent_b,
)

# Agent B recalls through the shared view — scope-bounded
results = await client.recall_shared(
    agent_id=agent_b,
    view_id=view["id"],
    query="how do I load CSV files safely?",
)
# All result atom IDs must appear in snapshot_atoms for this view.
```

---

## What NOT to build today

- Consolidation job (decay, merge, purge) — not needed for the demo
- Agent departure endpoint — already exists, no changes needed
- Skill files (`claude_skill.md`) — write after you've seen real export output
- PyPI packaging — after the repo is consolidated
- `POST /v1/agents/provision` (autonomous provisioning) — future
- Any UI
