# Mnemo v2 Phase 1: design note (gate G1)

**Date:** 2026-11-22 · **Branch:** `v2/phase1` (off `v2/phase0`) · **Spec:** `docs/mnemo-v2-phas1-spec.md`

This note covers spec items 3–7: migration 010, request/response shapes, the store path, decomposer v2 (with the full prompt), the deterministic checks, the read-side additions and the MCP tool signatures. No feature code is written before G1. Numbered **Open questions** at the end need Tom's call; everything else is a proposal to accept or amend.

Out of scope (spec rule 6): dedup and supersession logic, ranking, sharing, confidence recalibration, decision models in the write/read path. Dedup runs unchanged (`_check_duplicate` / `_merge_duplicate`, threshold 0.97 in production).

---

## 1. Migration `010_sources.sql`

```sql
-- v2 Phase 1: provenance. Every new atom points at the text it came from.
CREATE TABLE sources (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    store_id      UUID NOT NULL,
    text          TEXT NOT NULL,
    speaker       TEXT NOT NULL,
    addressee     TEXT,
    channel       TEXT,
    origin_class  TEXT NOT NULL DEFAULT 'unspecified'
                  CONSTRAINT sources_origin_class_check CHECK (origin_class IN (
                      'user_stated', 'professional', 'measurement',
                      'document', 'agent_inference', 'unspecified')),
    remembered_on TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_sources_store ON sources (store_id);
CREATE INDEX idx_sources_agent ON sources (agent_id, created_at);

ALTER TABLE atoms
    ADD COLUMN source_id    UUID REFERENCES sources(id) ON DELETE SET NULL,
    ADD COLUMN source_quote TEXT,
    ADD COLUMN verification JSONB;
CREATE INDEX idx_atoms_source ON atoms (source_id) WHERE source_id IS NOT NULL;

-- The source row now holds the text of a failed store; store_failures keeps
-- the error only for new rows.
ALTER TABLE store_failures ALTER COLUMN original_text DROP NOT NULL;

-- mnemo_migrator owns the tables; the app role needs explicit DML grants.
GRANT SELECT, INSERT, UPDATE, DELETE ON sources TO mnemo;
```

- **No backfill.** Existing atoms keep `source_id` NULL.
- **`sources.store_id` has no FK to `store_jobs`.** If job rows are ever pruned, a cascade must not delete source text out from under atoms. A unique index is enough.
- **Cost on production.** 12,898 atoms. Adding nullable columns with no default is a catalog-only change. The FK and partial index build in well under a second. The migration runs with the server stopped at G2.
- `schema.sql` is not changed: fresh databases get 010 from `mnemo migrate`, as with 006–009. `tests/conftest.py _CLEAN` adds `DELETE FROM sources` (after atoms, before agents).
- **`verification` shape:**
  `{"checks": "v1", "flags": ["abbreviation_altered"], "quote_found": true, "band_downgraded": true}`.
  Empty `flags` means every check passed. NULL means the atom predates Phase 1.

## 2. `POST /v1/agents/{agent_id}/remember`

### Request

```python
class RememberRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=50_000)          # unchanged
    domain_tags: list[str] = Field(default=[], max_length=20)       # unchanged
    remembered_on: Optional[datetime] = None                        # unchanged
    speaker: Optional[str] = Field(None, min_length=1, max_length=200)
    addressee: Optional[str] = Field(None, min_length=1, max_length=200)
    channel: Optional[str] = Field(None, min_length=1, max_length=200)
    origin_class: Literal["user_stated", "professional", "measurement",
                          "document", "agent_inference", "unspecified"] = "unspecified"
    wait: bool = True
```

- `speaker` defaults to the calling agent's `name`.
- `wait` goes in the JSON body (not a query parameter), alongside the other fields.

### Response, `wait=true`: 201

```json
{"status": "complete", "store_id": "…", "source_id": "…",
 "atoms_created": 3, "duplicates_merged": 1,
 "atoms": [{"id": "…", "atom_type": "episodic", "text_content": "…",
            "remembered_on": "2026-11-20T00:00:00Z", "source_quote": "…",
            "merged_into_existing": false, "flags": []}],
 "warnings": []}
```

- **Merged atoms.** For an atom that deduplicated into an existing one, `id` is the existing atom and `merged_into_existing` is true. `text_content` is the existing atom's text.
- **Flags on merged atoms.** These are computed on the decomposed atom and returned, but not written. The existing atom's `verification` is not touched, because dedup is out of scope.
- **`warnings`** holds plain strings, e.g. `"skipped 1 malformed atom from the decomposer"` or `"input split into 3 parts"`.
- **Status code.** 201 is unchanged, so 0.5.0 clients (which read only `store_id`) keep working against the new server.

### Response, `wait=false`: 201

The response is `{"status": "queued", "store_id": "…", "source_id": "…"}`; this is today's behaviour, plus `source_id`.

### Failure: 502

```json
{"status": "failed", "store_id": "…", "source_id": "…",
 "error": "decomposer failed after 3 attempts: provider timeout"}
```

- `error` is a plain, fixed-vocabulary message: timeout, rate limited, provider error, truncated response, no tool call. It never contains source text or model output.
- The source row stays, and `store_jobs.status = 'failed'`.

### New endpoints

- **`POST /v1/stores/{store_id}/retry`.**
  - Same auth as the status endpoint, plus the caller must be the owning agent (or its operator, or an admin).
  - Allowed only when `status = 'failed'`; otherwise 409.
  - Re-reads `sources.text` and the stored speaker/addressee/channel/origin_class/remembered_on, then runs the same synchronous path with the same `store_id` and `source_id`.
  - Safe to repeat: a failed store wrote no atoms, because all inserts are one transaction.
- **`GET /v1/stores/{store_id}/status`** adds `source_id` and `atoms` (same item shape as above, from `atoms WHERE source_id = …`).
  - Only new atoms carry `source_id`. Merged-into atoms aren't listed here; `atoms_created` / `duplicates_merged` give the counts.

## 3. Store path

```
route ── tx1: store_jobs(pending) + sources row          (connection released)
      ── decompose (model call, no connection held)
      ── encode_batch(all atom texts)  (no connection held)
      ── deterministic checks           (pure Python)
      ── tx2: dedup check + atom inserts + similarity/cross-call/arc edges
              + decomposer_usage + store_jobs(complete)
      ── response
      └─ background: lifecycle detection for new atom ids
```

- **Implementation.** `atom_service.store_from_text` splits into `prepare_atoms(...)` (no conn: decompose, embed, check) and `persist_atoms(conn, prepared, ...)` (one transaction). `store_background` becomes `run_store(pool, store_id, source)`. It is awaited for `wait=true`, and `asyncio.create_task` is used for `wait=false`.
- **Failure handling.**
  - A decomposer failure sets `store_jobs.status='failed'` plus the error, inserts a `store_failures` row with `original_text = NULL` and the trace, and returns 502.
  - A database failure in tx2 rolls back every atom of the store, then follows the same failure handling with a 500.
- **Background tasks.** A module-level `set[asyncio.Task]` holds references, and `task.add_done_callback(set.discard)`. On shutdown the lifespan waits up to 10 s for them and then cancels the rest.
- **Stuck jobs.** On startup, `store_jobs` rows in `pending`/`decomposing` older than 10 minutes become `failed` with the error `"interrupted (server restart)"`. They can be retried.
- **Lifecycle detection does not hold a connection during its model call.** Production runs with `MNEMO_LIFECYCLE_DETECTION_ENABLED=true`. `detect_lifecycle_relationships` is restructured into:
  - fetch candidates (acquire, query, release);
  - call the model per candidate (no connection);
  - write edges and DLQ rows (acquire, one transaction, release).

  Its logic and thresholds are unchanged.
- **Test for "no connection held".** The fake decomposer and `encode_batch` assert `pool.get_idle_size() == pool.get_size()` (or that the store's connection is not checked out) while they run.
- **Old-client timeout.** `mnemo-ai` 0.5.0 uses a 30 s HTTP timeout. Typical notes are well under 8,000 characters, which is one model call. Longer inputs are decomposed in parallel (at most 4 parts at once). If a call still exceeds the client timeout, the server completes the store anyway, and the atoms are visible via `/stores/{id}/status`. The new client uses a 120 s timeout for `remember`.
- `sync_store_for_tests` remains only for `wait=false` tests.

## 4. Decomposer v2

### Interface

```python
@dataclass
class DecomposeRequest:
    text: str
    agent_name: str
    speaker: str
    addressee: str | None
    channel: str | None
    remembered_on: datetime | None        # request date; None → today (UTC)

@dataclass
class CandidateAtom:
    text: str
    atom_type: Literal["episodic", "semantic", "procedural"]
    confidence: float                      # 0..1, mapped to Beta bands
    remembered_on: datetime | None
    source_quote: str
    subject: str | None
    entity_resolved: bool = True

@dataclass
class DecomposeResult:
    atoms: list[CandidateAtom]
    usage: list[dict]                      # one per model call
    warnings: list[str]
    version: str                           # "provider:model:prompt_v2"

class Decomposer(Protocol):
    provider: str
    model: str
    async def decompose(self, req: DecomposeRequest) -> DecomposeResult: ...

class DecomposerError(Exception): ...      # carries a plain-text reason
```

- **Package:** `mnemo/server/decomposers/` with `base.py`, `anthropic.py`, `openrouter.py`, `regex.py`, `fake.py`, `prompt_v2.py`, `split.py`.
- **Settings:**
  - `MNEMO_DECOMPOSER_PROVIDER` is `anthropic | openrouter | regex | fake`, default `anthropic`. The Phase 1 item 1 interim value `auto` is removed.
  - `MNEMO_DECOMPOSER_MODEL` defaults to `claude-haiku-4-5-20251001`, the model production runs today. A model change is a G2 decision informed by the bake-off fixture run.
- **Startup refusals:**
  - `anthropic` without `ANTHROPIC_API_KEY`. Today this silently falls back to regex.
  - `fake`, or a fake embedder, without a test DB (done in item 1).
  - `openrouter` unless `MNEMO_ALLOW_NON_ANTHROPIC_DECOMPOSER=true` **and** the database URL names `mnemo_eval` or a test DB. This enforces spec rule 4 in code. The production container never sets the flag.
- **Regex** runs only when the provider is explicitly `regex`. `decomposer_version` is `regex::regex_v1`.
- **`decomposer_usage`.** One row per model call. Columns are unchanged; the cache columns will read 0/NULL.

### Anthropic implementation

- **Structured output.** Forced tool use: `tools=[RECORD_ATOMS]`, `tool_choice={"type": "tool", "name": "record_atoms"}`. Input schema:

  ```json
  {"type": "object", "required": ["atoms"],
   "properties": {"atoms": {"type": "array", "items": {
     "type": "object",
     "required": ["text", "type", "subject", "source_quote", "confidence"],
     "properties": {
       "text": {"type": "string"},
       "type": {"enum": ["episodic", "semantic", "procedural"]},
       "subject": {"type": "string"},
       "source_quote": {"type": "string"},
       "confidence": {"type": "number", "minimum": 0, "maximum": 1},
       "remembered_on": {"type": ["string", "null"]},
       "entity_resolved": {"type": "boolean"}}}}}}
  ```

- **Validation.** Each item is validated with pydantic. An invalid item is skipped and adds a warning; the rest of the batch is kept.
- **Retries.** `AsyncAnthropic(max_retries=2, timeout=60.0)`. The SDK retries connection errors, 408, 409, 429 and 5xx with exponential backoff, which gives three attempts. 4xx errors other than those raise `DecomposerError` immediately.
- **Stop reason.** `"tool_use"` is required. `"max_tokens"` raises `DecomposerError("truncated response")`. A response with no `record_atoms` block raises `DecomposerError("no tool call")`. `max_tokens=8192` per part.
- **No `cache_control`** and **no regex fallback**.
- **Kept from v1:**
  - Confidence → Beta bands (`_confidence_to_beta`).
  - `entity_resolved=false` lowers confidence by 0.2.
  - The state-claim backstop that retypes semantic → episodic.
  - The per-atom `remembered_on`, falling back to the request date.

### Splitting

- Inputs over 8,000 characters are split on blank-line paragraph boundaries and greedily packed into parts of at most 8,000 characters.
- A single paragraph over 8,000 characters is split on sentence ends, and as a last resort on whitespace.
- Each part gets the same preamble plus `"This is part k of n of one note."`.
- At most 4 parts are decomposed at once. If any part fails, the whole store fails: no partial stores.
- Quotes are checked against the full source, not just the part.

### Prompt v2: full text

The system prompt is built per request. `{…}` placeholders are filled from the request. The preamble lines in brackets appear only when the field is present.

```text
This note was written by {speaker}. "I", "me" and "my" in the note mean {speaker}.
[If addressee:] The note is addressed to {addressee}. "You" and "your" in the note mean {addressee}.
[If no addressee:] The note names no addressee. If it says "you", write "the addressee" — do not guess who that is.
[If speaker != agent_name:] The note is being stored by the agent {agent_name} on {speaker}'s behalf.
[If channel:] The note came from: {channel}.
The note is dated {date, e.g. Friday, 20 November 2026}. Resolve every relative date against this date.

You turn the note into memory atoms: short, standalone claims that will be read later by someone who cannot see the note.

Rules for every atom:
1. Name the subject. Start from who or what the claim is about, by name ("Tom prefers…", "The billing service…"), never "I", "you", "he", "she", "it", "they" or "this". Fill in the subject from the preamble and the note.
2. A remark made to someone about themselves is a fact about that person. "You seem tired after the launch" addressed to Sam is about Sam ("Sam seemed tired after the launch"), not about the launch.
3. Keep each person's state with that person. Something the writer feels, plans or did is about the writer. Never record the writer's state, feelings, plans or actions as the addressee's or the user's, and never the reverse.
4. Copy abbreviations, tickers, codes, identifiers and proper nouns exactly as written, with the same capitalisation. Never expand an abbreviation, never replace a ticker with a company name, never correct a spelling you are unsure of.
5. One coherent claim per atom. Do not split facts that only make sense together. Do not generalise: keep numbers, names, dates and quantities exactly.
6. Resolve vague references ("the project", "that meeting", "the issue") from the note so the atom stands alone. If the note does not say what it refers to, keep the wording and set entity_resolved to false.
7. Resolve every relative time ("yesterday", "next month", "two weeks ago") to an absolute date using the note's date. Every episodic atom says when it happened or was observed.
8. A plan or intention ("will", "plans to", "next week") is its own atom, with the planned date. Do not record a plan as something that has already happened.
9. source_quote is the shortest passage copied character-for-character from the note that supports the atom. Do not paraphrase, shorten with "...", or join separate passages. If no single passage supports the atom, do not create the atom.
10. If the note is a conversation about a system, a tool or this memory service itself, record only the decisions, facts and plans it reaches — not the back-and-forth, not who asked what, not opinions that were later dropped.

Types:
- episodic: tied to a time — an event, an observation, a decision, a plan, or a state that could change ("X is currently Y"). If the claim could stop being true by the time someone reads it, it is episodic.
- semantic: true regardless of when it is read ("PostgreSQL supports partial indexes").
- procedural: a rule or instruction for future behaviour ("Always pin the dtype when reading this file").

Fields:
- text: the atom, one or two sentences.
- type: episodic | semantic | procedural.
- subject: the named person, system or thing the atom is about.
- source_quote: as rule 9.
- confidence: 0 to 1 — how firmly the note states it (a hedge such as "I think" or "maybe" lowers it).
- remembered_on: YYYY-MM-DD when the event or observation happened (for a plan, the planned date); required for episodic, null otherwise.
- entity_resolved: false only under rule 6.

Record the atoms with the record_atoms tool. If the note contains nothing worth remembering, call it with an empty list.
```

The user turn is the note text alone, or the part text for a split note. The v1 prompt's LoCoMo-specific rules and examples are dropped: "15–30 facts per conversation", the reaction-to-news rule, and the named examples. The date-resolution and `remembered_on` rules are kept.

## 5. Deterministic checks (`mnemo/server/services/grounding.py`)

Pure functions, unit-tested on synthetic strings. They run on every decomposed atom before persisting.

| flag | rule |
|---|---|
| `ungrounded_quote` | Normalise both strings: Unicode NFKC, casefold, curly quotes and dashes mapped to ASCII, whitespace runs collapsed to one space, trimmed. Flag if the quote is empty or is not a substring of the normalised source. |
| `abbreviation_altered` | For each token in the quote matching `\b[A-Z]{2,6}\b` (all capitals, 2–6 letters), flag if that exact token, case-sensitive and whole-word, does not appear in the atom text. |
| `no_subject` | Flag if the atom's first word (ignoring leading punctuation) is a pronoun or demonstrative: I, me, my, you, your, he, him, his, she, her, it, its, we, our, they, them, their, this, that, these, those. Also flag if the text contains no capitalised word after the first, no all-capitals token and no digit, i.e. nothing that names anyone. |

- **Effect.** A flagged atom is stored one band lower on the existing ladder: Beta(8,1) → (4,2) → (3,2) → (2,3) → (2,4), with the floor at (2,4). Multiple flags still lower it one band only. The flags go into `verification.flags` and the response.
- **Atoms are never dropped by a check.** Decomposer output that the prompt says should not exist (e.g. no supporting passage) shows up as `ungrounded_quote` rather than being filtered.
- **Phase 2 data.** The share of atoms flagged, broken down by flag (Phase 2 question 1), comes from `verification` counts.

## 6. Read side

- **`RetrieveRequest`** gains:
  - `since: datetime | None`
  - `until: datetime | None`
  - `atom_types: list[Literal["episodic","semantic","procedural"]] | None`

  The dates are compared against `COALESCE(a.remembered_on, a.created_at)`. `since` is inclusive and `until` exclusive. 422 if `since >= until`.
- **`POST /v1/agents/{agent_id}/recent`:**
  - Request body:

    ```python
    class RecentRequest(BaseModel):
        limit: int = Field(20, ge=1, le=100)
        since: datetime | None = None
        until: datetime | None = None
        atom_types: list[Literal["episodic","semantic","procedural"]] | None = None
        domain_tags: list[str] | None = None
        include_superseded: bool = False
    ```

  - Active atoms ordered by `COALESCE(remembered_on, created_at) DESC, created_at DESC`.
  - Response `{"atoms": [AtomResponse…], "total_returned": n}`.
  - No embedding call and no `access_count`/`last_accessed` update. Logged in `operations` as `recent`.
- **`GET /v1/agents/me`.** Agent key only; 403 for operator and admin keys. Returns `{"id", "name", "address", "status"}`. It is registered before `/agents/{agent_id}`, so `me` is never parsed as an identifier.
- **`AtomResponse`** gains `source_id: UUID | None` and `origin_class: str | None`, via `LEFT JOIN sources` in the recall, recent and get-atom queries. NULL for pre-Phase-1 atoms.
- **Agent identifiers in paths.** Today the path accepts a UUID or an address (`name:user.org`). Phase 1 adds a bare **name**, resolved within the caller's operator.
  - If two agents of that operator share the name, the response is 409. The error lists the accepted forms and tells the caller to use the address.
  - This is server-side so that every client gets it.

## 7. MCP server (`mnemo-client`, branch `v2/phase1`)

**Agent resolution.** `agent_id` is optional everywhere. The default order is:
1. the explicit argument (UUID, name or address, passed through to the server);
2. `MNEMO_DEFAULT_AGENT_ID` if set;
3. `GET /agents/me`, cached for the process lifetime.

Errors read: `"agent_id must be a UUID, an agent name (e.g. clio) or an address (clio:tom.inforge)"`. Startup validation uses `/agents/me` when no default is configured.

**Tools.** Existing tools keep their names and arguments unless listed here.

```python
mnemo_remember(text: str, agent_id: str | None = None,
               domain_tags: list[str] | None = None,
               remembered_on: str | None = None,          # ISO-8601 date or datetime
               speaker: str | None = None,
               addressee: str | None = None,
               channel: str | None = None,
               origin_class: Literal["user_stated","professional","measurement",
                                     "document","agent_inference","unspecified"] = "unspecified") -> str
```

Example output:

```
Stored 3 new, 1 merged (store_id 1f…).
- [episodic] Tom moved the release to 2026-12-03.
- [semantic] The billing service retries failed charges three times.  ⚑ abbreviation_altered
- [episodic, merged] …
```

On a 502 the output is `Not stored: <error>. The text was kept (store_id …); retry with mnemo_store_status/retry.`

```python
mnemo_recall(query: str, agent_id: str | None = None,
             domain_tags: list[str] | None = None,
             max_results: int = 5, min_similarity: float = 0.15,
             similarity_drop_threshold: float | None = 0.3,
             since: str | None = None, until: str | None = None,
             atom_types: list[str] | None = None,
             verbosity: Literal["full", "summary"] = "full",
             max_chars_per_atom: int = 600,
             max_total_tokens: int | None = 1500) -> str
```

- `full` maps to server `verbosity="truncated"` with `max_content_chars=max_chars_per_atom`, so every atom is complete up to the cap. `summary` keeps today's first-sentence mode.
- Each line shows the date: `[episodic 2026-11-20] …`.

```python
mnemo_recent(agent_id: str | None = None, limit: int = 10,
             since: str | None = None, until: str | None = None,
             atom_types: list[str] | None = None,
             domain_tags: list[str] | None = None) -> str
mnemo_forget(atom_id: str, agent_id: str | None = None) -> str     # DELETE …/atoms/{id}; audited deleted_by_agent
mnemo_store_status(store_id: str, retry: bool = False) -> str     # status + atoms; retry=True calls /retry on a failed store
```

- **Instructions string.** States that `remember` returns what was stored, and that `speaker`/`addressee` should be set when storing what someone else said. It lists the new tools.
- **README** updated. The version becomes **0.6.0**, and the `remember` HTTP timeout becomes 120 s.
- **`inforge-ai/mnemo-mcp`** gets a README notice ("superseded by `mnemo-ai[mcp]`") via a PR on that repo.
- **Release order.** The server deploys (G2) before the client is published (G3). The 0.6.0 client needs `/agents/me` and the new response fields, and 0.5.0 keeps working against the new server.

## 8. Tests per item

- **010.** A migration test runs `apply_pending` in a rolled-back transaction (the Phase 0 pattern). It checks the constraints: `origin_class` CHECK, SET NULL on source delete, cascade on agent delete.
- **Store path:**
  - a `wait=true` response lists the stored atoms, and an immediate recall finds them;
  - `wait=false` still queues;
  - a scripted `DecomposerError` gives 502, leaves the source row, writes no atoms and sets the job to failed, and retry then succeeds;
  - a startup sweep of stuck jobs;
  - no connection held during the decompose and embed calls;
  - background task references are kept.
- **Decomposer.** Against a mocked `AsyncAnthropic`:
  - tool-use parsing, with one malformed item skipped;
  - `max_tokens` → failure;
  - retry counts;
  - no `cache_control`;
  - the preamble built for each speaker/addressee combination;
  - a split of a 20,000-character input;
  - a `version` string.
- **Checks.** A table-driven test per flag, plus the band downgrade.
- **Read side:** `since`/`until`/`atom_types` filters, `recent` ordering with no access update, `/agents/me`, name resolution with the ambiguity error, and `source_id`/`origin_class` in responses.
- **Fixtures (item 8):** live, 5/5, under `-m eval`.

## Open questions for Tom

1. **Source text retention.** `sources.text` keeps the full note even after the agent forgets every atom from it (`mnemo_forget` soft-deletes atoms only). Options:
   - keep it (it is the audit trail);
   - delete the source when its last atom is forgotten;
   - leave it to a later phase.

   *Proposed:* keep it in Phase 1 and decide retention in a later phase.
2. **Existing `store_failures.original_text`.** Production has old rows with full text. *Proposed:* leave them; they're out of scope and admin-only. Purging them would be a separate data change (dry-run → STOP).
3. **No addressee.** For notes with no addressee, the prompt tells the model to write "the addressee" rather than guess. Alternative: default `addressee` to "the user" for agent-written notes. *Proposed:* no default. The fixtures will show whether that hurts.
4. **`subject` field.** The tool schema asks for `subject` separately to force rule 1. *Proposed:* store it in `atoms.structured->>'subject'` (no new column) and don't use it for ranking.
5. **Decomposer model.** Phase 1 ships with Haiku 4.5 (unchanged). The fixture run on Haiku 4.5 and the current Haiku (`claude-haiku-5-5`) goes into the bake-off doc. Any switch is proposed at G2.
6. **Bare-name resolution** in agent paths (§6). OK to add, or keep to UUID/address on the server and resolve names in the client?
