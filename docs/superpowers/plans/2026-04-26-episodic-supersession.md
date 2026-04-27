# Lifecycle Relationship Detection + Structured Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `_filter_superseded()` in `mnemo/server/services/atom_service.py:909` actually do something, and extend Mnemo's edge ontology with two new lifecycle types (`tension_with`, `narrows`) so the recall path can either filter (supersedes) or surface-with-context (tension/narrows). Prerequisite: structured JSON logging.

**Spec source:** `docs/episodic_suppression-tension.md` (Tom Davis, 2026-04-26). This spec supersedes the earlier "Supersession Detection" draft.

**Architecture:** Three independently-mergeable PRs:
1. **PR 1 — Structured logging.** Stdlib `logging` + JSON formatter, configured on FastAPI startup. No new dep. Unchanged from prior plan revision.
2. **PR 2 — Failing eval set.** Nine canonical scenarios (3 supersedes, 2 tensions, 1 narrows, 3 negative controls), all `xfail(strict=True)`. Forcing function for PR 3.
3. **PR 3 — Lifecycle service + recall metadata.** New `lifecycle_service.py` with 4-way classifier (supersedes / tension_with / narrows / independent), asymmetric thresholds, "no competing edges" idempotency, feature-flagged hook in `store_background`, dead-letter table for transient Haiku failures, and a recall-path extension that attaches `lifecycle_edges` metadata to each surviving atom.

**Tech stack:** Python 3.12, FastAPI, asyncpg, pgvector (ivfflat cosine), Anthropic SDK (`claude-haiku-4-5-20251001`), pytest + pytest-asyncio + respx, stdlib `logging`.

**Spec deviations / corrections (folded into this plan):**
- Spec says `_filter_superseded` is in `routes/memory.py`; it is actually in `mnemo/server/services/atom_service.py:909`.
- Spec references `consolidation_service.py`; the file is `consolidation.py`.
- Eval lands at `tests/eval/test_lifecycle_eval.py` (this repo's tests live at top-level `tests/`, not under `mnemo/server/`).
- Edges table currently lacks a `metadata` column; migration `006` adds it. The same migration extends the `edge_type` CHECK constraint to allow `tension_with` and `narrows`.
- `models.py:143` declares a closed `Literal[...]` for edge types; PR 3 extends it with the two new types.
- Spec's Task 4 calls for "single retry on transient error, then enqueue to dead-letter." Implemented as one in-process retry on `asyncio.TimeoutError` / network errors; permanent failures (post-retry) write to the DLQ table.

---

## File Structure

**Phase 1 — Logging (unchanged from prior revision):**
- Create: `mnemo/server/logging_config.py` — `JsonFormatter` + `configure_logging()`.
- Modify: `mnemo/server/main.py:1-49` — call `configure_logging()` at the top of `lifespan()`.
- Modify: `mnemo/server/config.py` — add `log_level: str = "INFO"`.
- Create: `tests/test_logging_config.py` — unit + integration smoke tests.

**Phase 2 — Eval:**
- Create: `tests/eval/__init__.py`.
- Create: `tests/eval/test_lifecycle_eval.py` — nine `@pytest.mark.xfail(strict=True)` scenarios.
- Modify: `pyproject.toml` `[tool.pytest.ini_options]` — register `eval` marker, `addopts = "-m 'not eval'"`.

**Phase 3 — Service + recall extension:**
- Create: `migrations/006_lifecycle_edges.sql` — `ALTER TABLE edges DROP CONSTRAINT … ADD CONSTRAINT … CHECK (...)` to allow `tension_with` and `narrows`; `ADD COLUMN metadata JSONB`; `CREATE TABLE lifecycle_dlq`.
- Modify: `mnemo/server/models.py:141-143` — extend the `edge_type` `Literal[...]`; add a `LifecycleEdge` response model and an optional `lifecycle_edges` field on `AtomResponse`.
- Modify: `mnemo/server/services/atom_service.py:958-977` — extend `create_edge()` to accept `metadata: dict | None = None`.
- Modify: `mnemo/server/services/atom_service.py:609-614` — `store_from_text` returns `new_atom_ids: list[UUID]`.
- Modify: `mnemo/server/services/atom_service.py:617-680` (`store_background`) — feature-flagged post-transaction hook calls `lifecycle_service.detect_lifecycle_relationships`.
- Modify: `mnemo/server/services/atom_service.py` (`retrieve()` near the end, around line 900) — after building `all_atoms`, attach `lifecycle_edges` to each returned atom by querying `edges` for `tension_with` / `narrows` types touching the result-set ids.
- Create: `mnemo/server/services/lifecycle_service.py` — `_get_candidates`, `_pair_has_lifecycle_edge`, `_evaluate_pair`, `detect_lifecycle_relationships`, `_record_dlq`.
- Modify: `mnemo/server/config.py` — add lifecycle settings (band, candidate limit, three thresholds, timeout, feature flag).
- Modify: `tests/conftest.py` — set `MNEMO_LIFECYCLE_DETECTION_ENABLED=true` for tests; add `lifecycle_dlq` to the `_CLEAN` SQL.
- Create: `tests/test_lifecycle_service.py` — DB-backed unit tests with the LLM mocked.
- Create: `tests/test_recall_lifecycle_metadata.py` — integration test that the recall response carries `lifecycle_edges`.
- Modify: `tests/eval/test_lifecycle_eval.py` — remove `xfail` markers (final step of Phase 3).

---

# Phase 1 — Structured Logging (PR 1)

### Task 1: JSON formatter and `configure_logging()`

**Files:**
- Create: `mnemo/server/logging_config.py`
- Modify: `mnemo/server/config.py:5-67`
- Test: `tests/test_logging_config.py`

- [ ] **Step 1: Write the failing unit tests for the formatter**

Create `tests/test_logging_config.py`:

```python
"""Tests for structured JSON logging config."""
import io
import json
import logging

import pytest


def test_json_formatter_emits_required_fields():
    from mnemo.server.logging_config import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="my.module",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "my.module"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload
    assert payload["timestamp"].endswith("+00:00")


def test_json_formatter_passes_through_extra_fields():
    from mnemo.server.logging_config import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="my.module", level=logging.INFO, pathname=__file__,
        lineno=1, msg="event", args=(), exc_info=None,
    )
    record.event = "lifecycle_check"
    record.cosine = 0.73
    record.edge_created = True
    payload = json.loads(formatter.format(record))

    assert payload["event"] == "lifecycle_check"
    assert payload["cosine"] == 0.73
    assert payload["edge_created"] is True


def test_json_formatter_includes_exception_info():
    from mnemo.server.logging_config import JsonFormatter

    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="my.module", level=logging.ERROR, pathname=__file__,
            lineno=1, msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))

    assert payload["level"] == "ERROR"
    assert "exception" in payload
    assert "ValueError: boom" in payload["exception"]


def test_configure_logging_routes_records_through_json_formatter():
    from mnemo.server.logging_config import configure_logging, JsonFormatter

    buf = io.StringIO()
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        configure_logging(level="INFO", stream=buf)
        logging.getLogger("smoke").info("hi", extra={"event": "smoke", "k": 1})
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        line = buf.getvalue().strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["event"] == "smoke"
        assert payload["k"] == 1
        assert payload["message"] == "hi"
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
```

- [ ] **Step 2: Run the tests — they should fail with ImportError**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_logging_config.py -v`
Expected: 4 errors with `ModuleNotFoundError: No module named 'mnemo.server.logging_config'`.

- [ ] **Step 3: Implement `mnemo/server/logging_config.py`**

Create `mnemo/server/logging_config.py`:

```python
"""Structured JSON logging for the Mnemo server.

Stdlib `logging` with a custom formatter that emits one JSON object per record
to the configured stream (stdout in production; Docker/k8s captures it).

Call sites stay as `logger = logging.getLogger(__name__)` and pass structured
fields via `extra={...}`; the formatter merges them into the JSON payload.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TextIO

_RESERVED_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message",
})


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per LogRecord. Extra kwargs are merged at top level."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Replace root logger handlers with a single JSON-formatted stream handler.

    Idempotent: clears any existing handlers first. Safe to call multiple times.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
```

- [ ] **Step 4: Add `log_level` setting in `mnemo/server/config.py`**

Edit `mnemo/server/config.py`. Replace the block from `# Testing` to the end of the class:

```python
    # Testing
    sync_store_for_tests: bool = False  # if True, /remember awaits the store task inline

    # Logging
    log_level: str = "INFO"

    model_config = {"env_prefix": "MNEMO_", "env_file": ".env", "extra": "ignore"}
```

- [ ] **Step 5: Run the tests — should pass**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_logging_config.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add mnemo/server/logging_config.py mnemo/server/config.py tests/test_logging_config.py
git commit -m "$(cat <<'EOF'
feat(logging): add JsonFormatter + configure_logging()

Single-file structured logging using stdlib logging — no new dep. Extra kwargs
on log calls flow through as top-level JSON fields, which the upcoming
lifecycle service depends on for per-check observability.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Wire `configure_logging()` into FastAPI startup

**Files:**
- Modify: `mnemo/server/main.py:1-49`
- Test: `tests/test_logging_config.py`

- [ ] **Step 1: Append the lifespan integration test**

Append to `tests/test_logging_config.py`:

```python
@pytest.mark.asyncio
async def test_lifespan_configures_json_logging(monkeypatch):
    """The FastAPI lifespan must call configure_logging()."""
    from mnemo.server import main as main_module
    from mnemo.server.logging_config import JsonFormatter

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers = []
    root.setLevel(logging.WARNING)

    called = {"count": 0}
    real_configure = main_module.configure_logging

    def spy(*args, **kwargs):
        called["count"] += 1
        return real_configure(*args, **kwargs)

    monkeypatch.setattr(main_module, "configure_logging", spy)

    async def _noop_pool():
        class _Sentinel:
            async def close(self):
                return None
        return _Sentinel()

    monkeypatch.setattr(main_module, "create_pool", _noop_pool)
    monkeypatch.setattr(main_module, "close_pool", lambda: None)
    monkeypatch.setattr(main_module, "set_pool", lambda p: None)

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr("mnemo.server.embeddings.warmup", lambda: None)
    monkeypatch.setattr("mnemo.server.services.migration_service.run_migrations", _noop)

    async def _consolidation_noop(pool):
        return None
    monkeypatch.setattr(
        "mnemo.server.services.consolidation.consolidation_loop", _consolidation_noop,
    )

    try:
        async with main_module.lifespan(main_module.app):
            assert called["count"] == 1
            assert any(isinstance(h.formatter, JsonFormatter) for h in logging.getLogger().handlers)
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
```

- [ ] **Step 2: Run — should fail (no `configure_logging` import in main.py)**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_logging_config.py::test_lifespan_configures_json_logging -v`
Expected: AttributeError on `main_module.configure_logging`.

- [ ] **Step 3: Wire into `main.py`**

Edit `mnemo/server/main.py`. Replace lines 1-19 with:

```python
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .database import create_pool, close_pool, set_pool
from .logging_config import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(level=settings.log_level)

    from .embeddings import warmup
    await asyncio.get_event_loop().run_in_executor(None, warmup)
```

(Remove the now-redundant `from .config import settings` import at the original line 26 inside the lifespan body.)

- [ ] **Step 4: Run the tests — should pass**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_logging_config.py -v`
Expected: 5 passed.

- [ ] **Step 5: Run the full suite**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest -x`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add mnemo/server/main.py
git commit -m "$(cat <<'EOF'
feat(logging): configure JSON logging in FastAPI lifespan

Calls configure_logging(settings.log_level) at startup so the entire process
emits structured logs from the first request onward.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Open PR 1

- [ ] **Step 1: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --title "feat(logging): structured JSON logging" --body "$(cat <<'EOF'
## Summary
- Add `JsonFormatter` + `configure_logging()` in `mnemo/server/logging_config.py` — stdlib logging, no new dependency.
- Wire `configure_logging(settings.log_level)` into the FastAPI lifespan so every record from app startup onward emits one JSON line per event to stdout.
- Existing `getLogger(__name__)` call sites work unchanged; new `extra={...}` kwargs flow through as top-level JSON fields.

## Why now
Prerequisite for the upcoming lifecycle-detection work (`docs/episodic_suppression-tension.md` §6a). Per-check structured fields are how we'll know whether the new pipeline is firing, missing, or false-positiving once it lands.

## Test plan
- [x] Unit tests for the formatter — `tests/test_logging_config.py`
- [x] Integration test for lifespan wiring
- [x] Full suite still green

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

# Phase 2 — Failing Eval Set (PR 2)

### Task 4: Eval directory and pytest marker

**Files:**
- Create: `tests/eval/__init__.py`
- Modify: `pyproject.toml` `[tool.pytest.ini_options]` block

- [ ] **Step 1: Create empty marker**

Create `tests/eval/__init__.py` with empty content (single blank file).

- [ ] **Step 2: Register the `eval` marker and exclude from default runs**

Edit `pyproject.toml`. Replace the `[tool.pytest.ini_options]` block with:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
markers = [
    "eval: end-to-end eval cases that hit the live Anthropic API. Run with -m eval; excluded from default runs.",
]
addopts = "-m 'not eval'"
```

- [ ] **Step 3: Verify configuration**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest --collect-only -q | tail -5`
Expected: existing tests collected, no errors.

Run: `cd /home/tompdavis/mnemo-server && uv run pytest -m eval --collect-only -q`
Expected: `0 tests collected`.

- [ ] **Step 4: Commit**

```bash
git add tests/eval/__init__.py pyproject.toml
git commit -m "$(cat <<'EOF'
test: scaffold tests/eval/ with opt-in 'eval' pytest marker

Default pytest runs exclude eval; CI selects them with -m eval.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Nine lifecycle eval cases (xfail until Phase 3)

**Files:**
- Create: `tests/eval/test_lifecycle_eval.py`

- [ ] **Step 1: Write the eval suite**

Create `tests/eval/test_lifecycle_eval.py`:

```python
"""End-to-end lifecycle eval — nine cases from docs/episodic_suppression-tension.md.

These tests:
- Hit the live Haiku decomposer + (once implemented) the live lifecycle LLM.
- Run only with `pytest -m eval`. Slow and consumes Anthropic API budget.
- Are marked xfail(strict=True) until the lifecycle service ships in Phase 3,
  at which point the markers are removed.

Each case stores 1+ atoms via /remember (which awaits store_background inline
under MNEMO_SYNC_STORE_FOR_TESTS=true) then issues a /recall and asserts on
edge state in the DB and lifecycle_edges metadata in the recall response.
"""

import os
from uuid import UUID

import pytest

from tests.conftest import remember as remember_helper

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="eval requires ANTHROPIC_API_KEY",
    ),
    pytest.mark.xfail(
        strict=True,
        reason="lifecycle service not yet implemented (Phase 3)",
    ),
]


async def _recall(client, agent_key: str, agent_id: str, query: str, max_results: int = 10):
    headers = {"X-Agent-Key": agent_key}
    resp = await client.post(
        f"/v1/agents/{agent_id}/recall",
        json={"query": query, "max_results": max_results},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _edges_of_type(pool, agent_id: UUID, edge_type: str) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.source_id, e.target_id, e.weight, e.edge_type
            FROM edges e
            JOIN atoms src ON src.id = e.source_id
            JOIN atoms tgt ON tgt.id = e.target_id
            WHERE e.edge_type = $2
              AND src.agent_id = $1
              AND tgt.agent_id = $1
            """,
            agent_id, edge_type,
        )
    return [dict(r) for r in rows]


async def _lifecycle_edges_in_recall(result: dict, edge_type: str) -> list[dict]:
    found = []
    for atom in result.get("atoms", []):
        for edge in atom.get("lifecycle_edges") or []:
            if edge.get("relationship") == edge_type:
                found.append(edge)
    return found


# ── Case 1: State change (supersedes) ────────────────────────────────────────

async def test_case_1_state_change_supersedes(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Zulip integration is a planned future task", headers=headers)
    await remember_helper(client, aid, "Zulip integration is complete and in daily use", headers=headers)

    result = await _recall(client, agent_key, aid, "Zulip integration status")
    texts = " || ".join(a["text_content"] for a in result["atoms"]).lower()
    assert "complete" in texts
    assert "planned" not in texts, f"planned atom not superseded: {texts}"

    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert len(sup) >= 1


# ── Case 2: Preference change (supersedes) ───────────────────────────────────

async def test_case_2_preference_change_supersedes(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Tom prefers Mattermost for team communication", headers=headers)
    await remember_helper(client, aid, "Tom now prefers Zulip; Mattermost has been replaced", headers=headers)

    result = await _recall(client, agent_key, aid, "Tom communication preferences")
    texts = " || ".join(a["text_content"] for a in result["atoms"]).lower()
    assert "zulip" in texts
    assert "mattermost" not in texts or "replaced" in texts

    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert len(sup) >= 1


# ── Case 3: Dedup-by-rephrasing (control: NO lifecycle edge) ────────────────

async def test_case_3_dedup_by_rephrasing_no_edge(client, agent_with_key, pool):
    agent_data, _agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "test tasks consumed 89% of spend", headers=headers)
    await remember_helper(client, aid, "test tasks were cost black holes consuming 89%", headers=headers)

    for et in ("supersedes", "tension_with", "narrows"):
        edges = await _edges_of_type(pool, agent_uuid, et)
        assert edges == [], f"unexpected {et} edge in dedup band: {edges}"


# ── Case 4: Episodic correction (supersedes) ─────────────────────────────────

async def test_case_4_episodic_correction_supersedes(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Mnemo achieves 76.1% on LoCoMo benchmark", headers=headers)
    await remember_helper(
        client, aid,
        "Actually Mnemo achieves 82.1% on LoCoMo; 76.1% was the gte-small result",
        headers=headers,
    )

    result = await _recall(client, agent_key, aid, "Mnemo LoCoMo score")
    texts = " || ".join(a["text_content"] for a in result["atoms"])
    assert "82.1" in texts
    standalone_old = [
        a for a in result["atoms"]
        if "76.1" in a["text_content"] and "82.1" not in a["text_content"]
    ]
    assert standalone_old == [], f"old atom not superseded: {standalone_old}"

    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert len(sup) >= 1


# ── Case 5: Stale-but-not-superseded (control: NO edge, classified independent) ─

async def test_case_5_independent_no_edge(client, agent_with_key, pool):
    agent_data, _agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Tom is co-founder of Inforge LLC", headers=headers)
    await remember_helper(client, aid, "Inforge LLC was incorporated in Delaware in March 2023", headers=headers)

    for et in ("supersedes", "tension_with", "narrows"):
        edges = await _edges_of_type(pool, agent_uuid, et)
        assert edges == [], f"facet additions should not create {et}: {edges}"


# ── Case 6: Narrowing (narrows edge + lifecycle metadata in recall) ──────────

async def test_case_6_narrowing_creates_narrows_edge(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Tom uses Mattermost for all communication", headers=headers)
    await remember_helper(
        client, aid,
        "Tom uses Zulip for Inforge ops; Mattermost for personal",
        headers=headers,
    )

    result = await _recall(client, agent_key, aid, "Tom communication tools")
    texts = " || ".join(a["text_content"] for a in result["atoms"]).lower()
    assert "mattermost" in texts
    assert "zulip" in texts

    narrows = await _edges_of_type(pool, agent_uuid, "narrows")
    assert len(narrows) >= 1
    surfaced = await _lifecycle_edges_in_recall(result, "narrows")
    assert len(surfaced) >= 1, f"recall response missing lifecycle_edges narrows: {result}"


# ── Case 7: Semantic tension (tension_with, NOT supersedes) ──────────────────

async def test_case_7_semantic_tension_not_supersedes(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(
        client, aid,
        "Newtonian gravity accurately predicts planetary orbits",
        headers=headers,
    )
    await remember_helper(
        client, aid,
        "Mercury's perihelion precesses by 43 arcseconds per century beyond Newtonian prediction",
        headers=headers,
    )

    result = await _recall(client, agent_key, aid, "Newtonian gravity validity")
    texts = " || ".join(a["text_content"] for a in result["atoms"]).lower()
    assert "newtonian" in texts
    assert "mercury" in texts or "perihelion" in texts

    tension = await _edges_of_type(pool, agent_uuid, "tension_with")
    assert len(tension) >= 1
    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert sup == [], f"semantic claim incorrectly superseded: {sup}"
    surfaced = await _lifecycle_edges_in_recall(result, "tension_with")
    assert len(surfaced) >= 1


# ── Case 8: Benchmark tension (tension_with) ─────────────────────────────────

async def test_case_8_benchmark_tension(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(
        client, aid,
        "Mnemo achieves 82.1% on LoCoMo multi-hop, best-in-class",
        headers=headers,
    )
    await remember_helper(
        client, aid,
        "Hindsight achieves 91.4% on LongMemEval, exceeding Mnemo",
        headers=headers,
    )

    result = await _recall(client, agent_key, aid, "Mnemo competitive position on memory benchmarks")
    texts = " || ".join(a["text_content"] for a in result["atoms"]).lower()
    assert "mnemo" in texts
    assert "hindsight" in texts

    tension = await _edges_of_type(pool, agent_uuid, "tension_with")
    assert len(tension) >= 1
    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert sup == [], f"competitive benchmarks should not supersede: {sup}"


# ── Case 9: Episodic measurement correction (supersedes, NOT tension) ────────

async def test_case_9_episodic_measurement_supersedes(client, agent_with_key, pool):
    agent_data, agent_key, headers = agent_with_key
    aid = str(agent_data["id"])
    agent_uuid = UUID(aid)

    await remember_helper(client, aid, "Q3 revenue forecast is $4.2M", headers=headers)
    await remember_helper(
        client, aid,
        "Corrected Q3 revenue forecast is $3.8M; the $4.2M number had a calculation error",
        headers=headers,
    )

    result = await _recall(client, agent_key, aid, "Q3 revenue forecast")
    texts = " || ".join(a["text_content"] for a in result["atoms"])
    assert "$3.8M" in texts or "3.8M" in texts
    standalone_old = [
        a for a in result["atoms"]
        if "$4.2M" in a["text_content"] and "$3.8M" not in a["text_content"] and "3.8M" not in a["text_content"]
    ]
    assert standalone_old == [], f"old measurement not superseded: {standalone_old}"

    sup = await _edges_of_type(pool, agent_uuid, "supersedes")
    assert len(sup) >= 1
    tension = await _edges_of_type(pool, agent_uuid, "tension_with")
    assert tension == [], f"correction should not be a tension: {tension}"
```

- [ ] **Step 2: Run the eval — all should be xfail (or skipped without API key)**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/eval/ -m eval -v`
Expected: 9 xfailed (or 9 skipped if `ANTHROPIC_API_KEY` is unset).

- [ ] **Step 3: Run the default suite to confirm eval is excluded**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest -q | tail -5`
Expected: full suite passes; eval cases deselected.

- [ ] **Step 4: Commit**

```bash
git add tests/eval/test_lifecycle_eval.py
git commit -m "$(cat <<'EOF'
test(eval): add nine lifecycle eval cases (xfail until service ships)

Forcing function for lifecycle detection. Three supersedes cases (state change,
preference flip, episodic correction), one narrows, two tensions (semantic and
benchmark), three controls (dedup, independent, episodic-not-tension). All
xfail(strict=True) — once the service lands they xpass and the markers come
off in the same PR.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Open PR 2

- [ ] **Step 1: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --title "test(eval): lifecycle eval scaffold + nine failing cases" --body "$(cat <<'EOF'
## Summary
- Add `tests/eval/` with an `eval` pytest marker excluded from default runs.
- Nine canonical lifecycle cases (supersedes ×3, narrows ×1, tension_with ×2, controls ×3) per `docs/episodic_suppression-tension.md`, all `xfail(strict=True)`.
- Forcing function: when Phase 3 lands, the strict xfail flips to xpass and CI red until the markers are removed.

## Why
Spec rollout step 2: land the eval first as proof the bug exists, then implement against it.

## Test plan
- [x] `pytest -m eval -v` → 9 xfailed (or skipped if no ANTHROPIC_API_KEY)
- [x] Default `pytest` run excludes eval and stays green

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

# Phase 3 — Lifecycle Service + Recall Metadata (PR 3)

### Task 7: Migration — extend edge_type CHECK, add `metadata` JSONB, add `lifecycle_dlq` table

**Files:**
- Create: `migrations/006_lifecycle_edges.sql`

- [ ] **Step 1: Write the migration**

Create `migrations/006_lifecycle_edges.sql`:

```sql
-- Migration 006: Lifecycle relationship edges
--
-- Extends the edge_type allowlist with 'tension_with' and 'narrows';
-- adds nullable metadata JSONB to edges (LLM reasoning, detector version,
-- detection timestamp, cosine-at-detection); adds lifecycle_dlq table
-- for transient Haiku failures so the system degrades gracefully.

ALTER TABLE edges
    DROP CONSTRAINT IF EXISTS edges_edge_type_check;

ALTER TABLE edges
    ADD CONSTRAINT edges_edge_type_check
    CHECK (edge_type IN (
        'supports', 'contradicts', 'depends_on',
        'generalises', 'specialises', 'motivated_by',
        'evidence_for', 'supersedes', 'summarises', 'related',
        'tension_with', 'narrows'
    ));

ALTER TABLE edges
    ADD COLUMN IF NOT EXISTS metadata JSONB;

CREATE TABLE IF NOT EXISTS lifecycle_dlq (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    new_atom_id  UUID NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
    candidate_id UUID REFERENCES atoms(id) ON DELETE CASCADE,
    agent_id     UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    error        TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_dlq_agent_created
    ON lifecycle_dlq (agent_id, created_at DESC);

INSERT INTO schema_migrations (version) VALUES ('006_lifecycle_edges') ON CONFLICT DO NOTHING;
```

- [ ] **Step 2: Apply the migration to the test DB**

Run (substitute test DB name from `MNEMO_TEST_DATABASE_URL`):

```bash
cd /home/tompdavis/mnemo-server && \
  TESTDB="$(grep MNEMO_TEST_DATABASE_URL .env | cut -d= -f2- | tr -d '"' | sed 's|.*/||')" && \
  sudo -u postgres psql "$TESTDB" -f migrations/006_lifecycle_edges.sql
```

Expected: `ALTER TABLE`, `ALTER TABLE`, `CREATE TABLE`, `CREATE INDEX`, `INSERT 0 1`.

Also apply to your dev DB if you want to exercise endpoints locally.

- [ ] **Step 3: Sanity-check the constraint**

Run: `sudo -u postgres psql "$TESTDB" -c "\d edges"`
Expected: the `edges_edge_type_check` constraint lists `tension_with` and `narrows`; the `metadata jsonb` column is present.

- [ ] **Step 4: Commit**

```bash
git add migrations/006_lifecycle_edges.sql
git commit -m "$(cat <<'EOF'
feat(schema): add tension_with/narrows edge types, metadata JSONB, lifecycle_dlq

Migration 006 extends edge_type allowlist and adds nullable metadata
(reasoning, detector version, detection timestamp). lifecycle_dlq holds
records for which the lifecycle LLM call failed permanently.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Models — extend edge_type Literal, add LifecycleEdge response model

**Files:**
- Modify: `mnemo/server/models.py:141-143`

- [ ] **Step 1: Read current models**

Run: `cd /home/tompdavis/mnemo-server && sed -n '135,160p' mnemo/server/models.py`
Note the current shape of the `edge_type` Literal (line 141-143) and the `AtomResponse` / Edge models referenced.

- [ ] **Step 2: Extend the Literal**

Edit `mnemo/server/models.py`. Replace the `edge_type: Literal[...]` declaration (the one at line ~141-143) so its tuple of allowed values becomes:

```python
    edge_type: Literal[
        "supports", "contradicts", "depends_on", "generalises",
        "specialises", "motivated_by", "evidence_for", "supersedes",
        "summarises", "related", "tension_with", "narrows",
    ]
```

- [ ] **Step 3: Add `LifecycleEdge` and extend `AtomResponse`**

In `mnemo/server/models.py`, locate the `AtomResponse` model. Above it, add a new model:

```python
class LifecycleEdge(BaseModel):
    """Surfaced in recall responses for atoms that participate in
    tension_with or narrows relationships. supersedes edges are filtered
    server-side and never surface here."""
    related_atom_id: UUID
    relationship: Literal["tension_with", "narrows"]
    reasoning: str | None = None
    weight: float
```

Then extend `AtomResponse` by adding (just before its closing brace / at the end of the field block):

```python
    lifecycle_edges: list[LifecycleEdge] | None = None
```

(If `AtomResponse` is dict-shaped rather than a BaseModel, add the optional field in whatever shape matches the surrounding code. The retrieve path returns dicts via `_row_to_atom_response`; the response_model in `routes/memory.py` wraps it.)

- [ ] **Step 4: Run the suite to confirm the schema change is harmless**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest -x -q`
Expected: all passed (`lifecycle_edges` is None on every response — backwards-compatible).

- [ ] **Step 5: Commit**

```bash
git add mnemo/server/models.py
git commit -m "$(cat <<'EOF'
feat(models): extend edge_type Literal + add LifecycleEdge

tension_with and narrows are now valid edge types in the API surface.
AtomResponse gains an optional lifecycle_edges list to be populated by
the recall path in a follow-up commit.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: `create_edge()` accepts metadata

**Files:**
- Modify: `mnemo/server/services/atom_service.py:958-977`
- Test: `tests/test_create_edge_metadata.py`

- [ ] **Step 1: Write a failing unit test**

Create `tests/test_create_edge_metadata.py`:

```python
"""create_edge persists optional metadata JSONB."""
import json

import pytest


@pytest.mark.asyncio
async def test_create_edge_persists_metadata(pool, agent_with_address):
    from mnemo.server.services.atom_service import create_edge, _insert_atom
    from mnemo.server.decomposer import DecomposedAtom
    from mnemo.server.embeddings import encode

    agent_id = agent_with_address["id"]

    async with pool.acquire() as conn:
        emb_a = await encode("alpha fact one")
        emb_b = await encode("alpha fact two")
        a = await _insert_atom(
            conn, agent_id,
            DecomposedAtom(text="alpha fact one", atom_type="semantic",
                           confidence_alpha=4.0, confidence_beta=2.0),
            emb_a, ["t"], "direct_experience",
        )
        b = await _insert_atom(
            conn, agent_id,
            DecomposedAtom(text="alpha fact two", atom_type="semantic",
                           confidence_alpha=4.0, confidence_beta=2.0),
            emb_b, ["t"], "direct_experience",
        )
        result = await create_edge(
            conn=conn, source_id=a["id"], target_id=b["id"],
            edge_type="tension_with", weight=0.85,
            metadata={"reasoning": "test", "detector": "auto_lifecycle_v1"},
        )
        assert result is not None
        row = await conn.fetchrow(
            "SELECT metadata FROM edges WHERE id = $1", result["id"],
        )
        assert json.loads(row["metadata"]) == {
            "reasoning": "test",
            "detector": "auto_lifecycle_v1",
        }
```

- [ ] **Step 2: Run — should fail with TypeError**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_create_edge_metadata.py -v`
Expected: TypeError on `metadata` kwarg.

- [ ] **Step 3: Update `create_edge`**

Edit `mnemo/server/services/atom_service.py`. Replace the `create_edge` function (currently lines 958-977) with:

```python
async def create_edge(
    conn: asyncpg.Connection,
    source_id: UUID,
    target_id: UUID,
    edge_type: str,
    weight: float,
    metadata: dict | None = None,
) -> dict | None:
    row = await conn.fetchrow(
        """
        INSERT INTO edges (source_id, target_id, edge_type, weight, metadata)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (source_id, target_id, edge_type) DO NOTHING
        RETURNING id, source_id, target_id, edge_type, weight
        """,
        source_id,
        target_id,
        edge_type,
        weight,
        json.dumps(metadata) if metadata is not None else None,
    )
    return dict(row) if row else None
```

(Confirm `import json` exists at the top of the file; add it if missing.)

- [ ] **Step 4: Run — should pass**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_create_edge_metadata.py -v`
Expected: 1 passed.

- [ ] **Step 5: Run the full suite**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest -x -q`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add mnemo/server/services/atom_service.py tests/test_create_edge_metadata.py
git commit -m "$(cat <<'EOF'
feat(edges): create_edge accepts optional metadata

Backwards-compatible — existing call sites pass nothing and persist NULL.
Lifecycle service uses this to record LLM reasoning + detector version
on each edge.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: `store_from_text` returns `new_atom_ids`

**Files:**
- Modify: `mnemo/server/services/atom_service.py:609-614`
- Test: `tests/test_store_from_text_new_ids.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_store_from_text_new_ids.py`:

```python
"""store_from_text reports which atom IDs are newly inserted (vs merged)."""
import pytest


@pytest.mark.asyncio
async def test_store_from_text_returns_new_atom_ids_for_fresh_atoms(pool, agent_with_address):
    from mnemo.server.services.atom_service import store_from_text
    agent_id = agent_with_address["id"]

    async with pool.acquire() as conn:
        async with conn.transaction():
            r1 = await store_from_text(conn, agent_id, "Sky is blue.", ["t"])
        assert "new_atom_ids" in r1
        assert len(r1["new_atom_ids"]) == r1["atoms_created"]
        assert set(r1["new_atom_ids"]) == {a["id"] for a in r1["atoms"]}


@pytest.mark.asyncio
async def test_store_from_text_excludes_merged_duplicates_from_new_ids(pool, agent_with_address):
    from mnemo.server.services.atom_service import store_from_text
    agent_id = agent_with_address["id"]

    async with pool.acquire() as conn:
        async with conn.transaction():
            r1 = await store_from_text(conn, agent_id, "Pluto is a dwarf planet.", ["t"])
        async with conn.transaction():
            r2 = await store_from_text(conn, agent_id, "Pluto is a dwarf planet.", ["t"])
        assert r2["duplicates_merged"] >= 1
        assert r2["new_atom_ids"] == []
```

- [ ] **Step 2: Run — should fail with KeyError**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_store_from_text_new_ids.py -v`
Expected: 2 failures.

- [ ] **Step 3: Update `store_from_text`**

Edit `mnemo/server/services/atom_service.py`. Find the return dict at the end of `store_from_text` (around line 609) and replace it with:

```python
    new_atom_ids = [stored_ids[i] for i in new_atom_indices]

    return {
        "atoms": [_row_to_atom_response(r) for r in stored_rows],
        "atoms_created": atoms_created,
        "edges_created": edges_created,
        "duplicates_merged": duplicates_merged,
        "new_atom_ids": new_atom_ids,
    }
```

Also add a `"new_atom_ids": []` key to the early-return at line 507 so the contract is uniform:

```python
    if not decomposed:
        return {"atoms": [], "atoms_created": 0, "edges_created": 0, "duplicates_merged": 0, "new_atom_ids": []}
```

- [ ] **Step 4: Run — should pass**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_store_from_text_new_ids.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add mnemo/server/services/atom_service.py tests/test_store_from_text_new_ids.py
git commit -m "$(cat <<'EOF'
feat(atoms): expose new_atom_ids from store_from_text

The lifecycle hook needs to skip merged duplicates and only run on the
freshly-inserted atoms. Tracked via the existing new_atom_indices and
returned in the result dict.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Lifecycle config + service skeleton + candidate query

**Files:**
- Modify: `mnemo/server/config.py`
- Modify: `tests/conftest.py`
- Create: `mnemo/server/services/lifecycle_service.py`
- Test: `tests/test_lifecycle_service.py`

- [ ] **Step 1: Add lifecycle settings**

Edit `mnemo/server/config.py`. Just before the `# Logging` block, add:

```python
    # Lifecycle relationship detection (docs/episodic_suppression-tension.md).
    # Detection band [low, high) excludes off-topic pairs and dedup-band pairs.
    # Asymmetric thresholds: supersedes is destructive (silently retires an
    # atom), so it requires more LLM confidence than the additive types.
    lifecycle_detection_enabled: bool = False
    lifecycle_band_low: float = 0.50
    lifecycle_band_high: float = 0.90
    lifecycle_candidate_limit: int = 5
    lifecycle_llm_timeout_seconds: float = 5.0
    supersedes_threshold: float = 0.75
    tension_threshold: float = 0.65
    narrows_threshold: float = 0.65
```

- [ ] **Step 2: Enable lifecycle detection in tests**

Edit `tests/conftest.py`. Just below the `os.environ.setdefault("MNEMO_SYNC_STORE_FOR_TESTS", "true")` line, add:

```python
os.environ.setdefault("MNEMO_LIFECYCLE_DETECTION_ENABLED", "true")
```

Also add `"DELETE FROM lifecycle_dlq;"` near the top of the `_CLEAN` SQL string (before `DELETE FROM edges;`):

```python
_CLEAN = """
DELETE FROM agent_trust;
DELETE FROM capabilities;
DELETE FROM snapshot_atoms;
DELETE FROM lifecycle_dlq;
DELETE FROM edges;
DELETE FROM views;
... (rest unchanged)
"""
```

- [ ] **Step 3: Write a failing test for the candidate query**

Create `tests/test_lifecycle_service.py`:

```python
"""Unit tests for lifecycle_service. LLM call is mocked; DB is real."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mnemo.server.decomposer import DecomposedAtom
from mnemo.server.embeddings import encode


async def _insert(conn, agent_id, text, atom_type="semantic"):
    from mnemo.server.services.atom_service import _insert_atom
    emb = await encode(text)
    row = await _insert_atom(
        conn, agent_id,
        DecomposedAtom(text=text, atom_type=atom_type,
                       confidence_alpha=4.0, confidence_beta=2.0),
        emb, ["t"], "direct_experience",
    )
    return row["id"], emb


# ── Candidate query ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_candidates_filters_to_band(pool, agent_with_address):
    from mnemo.server.services.lifecycle_service import _get_candidates

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        new_id, new_emb = await _insert(conn, agent_id, "Zulip integration is complete and in daily use")
        same_id, _ = await _insert(conn, agent_id, "Zulip integration is a planned future task")
        off_id, _ = await _insert(conn, agent_id, "Pluto is a dwarf planet in the Kuiper belt")

        candidates = await _get_candidates(conn, agent_id, new_id, new_emb)

    cand_ids = {c["id"] for c in candidates}
    assert same_id in cand_ids
    assert off_id not in cand_ids
    for c in candidates:
        assert "text_content" in c and "similarity" in c and "atom_type" in c
        assert 0.50 <= c["similarity"] < 0.90


@pytest.mark.asyncio
async def test_get_candidates_excludes_self_and_inactive(pool, agent_with_address):
    from mnemo.server.services.lifecycle_service import _get_candidates

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        new_id, new_emb = await _insert(conn, agent_id, "Zulip integration is complete and in daily use")
        other_id, _ = await _insert(conn, agent_id, "Zulip integration is a planned future task")
        await conn.execute("UPDATE atoms SET is_active = false WHERE id = $1", other_id)
        candidates = await _get_candidates(conn, agent_id, new_id, new_emb)

    assert all(c["id"] != new_id for c in candidates)
    assert all(c["id"] != other_id for c in candidates)
```

- [ ] **Step 4: Run — should fail with ImportError**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_lifecycle_service.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 5: Implement the skeleton + candidate query**

Create `mnemo/server/services/lifecycle_service.py`:

```python
"""Lifecycle relationship detection (docs/episodic_suppression-tension.md).

Four-way classifier: supersedes / tension_with / narrows / independent.
Three are edge-creating; "independent" is a no-op. Runs from
atom_service.store_background after the store transaction commits, gated
by settings.lifecycle_detection_enabled. Failure mode: log and skip;
permanent failures land in lifecycle_dlq.
"""

import logging
from uuid import UUID

import asyncpg

from ..config import settings

logger = logging.getLogger(__name__)


async def _get_candidates(
    conn: asyncpg.Connection,
    agent_id: UUID,
    new_atom_id: UUID,
    embedding: list[float],
) -> list[dict]:
    """ANN-query active same-agent atoms, filter to the lifecycle cosine band."""
    over_fetch = max(settings.lifecycle_candidate_limit * 4, 20)
    rows = await conn.fetch(
        """
        SELECT id, text_content, atom_type, remembered_on, created_at,
               1 - (embedding <=> $1::vector) AS similarity
        FROM atoms
        WHERE agent_id = $2
          AND is_active = true
          AND id != $3
        ORDER BY embedding <=> $1::vector
        LIMIT $4
        """,
        embedding,
        agent_id,
        new_atom_id,
        over_fetch,
    )
    candidates = []
    for r in rows:
        sim = float(r["similarity"])
        if settings.lifecycle_band_low <= sim < settings.lifecycle_band_high:
            candidates.append({
                "id": r["id"],
                "text_content": r["text_content"],
                "atom_type": r["atom_type"],
                "remembered_on": r["remembered_on"],
                "created_at": r["created_at"],
                "similarity": sim,
            })
        if len(candidates) >= settings.lifecycle_candidate_limit:
            break
    return candidates
```

- [ ] **Step 6: Run — should pass**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_lifecycle_service.py -v`
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add mnemo/server/config.py tests/conftest.py mnemo/server/services/lifecycle_service.py tests/test_lifecycle_service.py
git commit -m "$(cat <<'EOF'
feat(lifecycle): config + candidate query

Settings: feature flag (default off), cosine band [0.50, 0.90),
asymmetric thresholds (supersedes 0.75, tension/narrows 0.65), 5s
LLM timeout. Tests opt in via MNEMO_LIFECYCLE_DETECTION_ENABLED.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: LLM caller `_evaluate_pair`

**Files:**
- Modify: `mnemo/server/services/lifecycle_service.py`
- Modify: `tests/test_lifecycle_service.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_lifecycle_service.py`:

```python
def _mock_haiku(payload_json: str, input_tokens: int = 100, output_tokens: int = 30):
    msg = MagicMock()
    msg.content = [MagicMock(text=payload_json)]
    msg.model = "claude-haiku-4-5-20251001"
    msg.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return msg


@pytest.mark.asyncio
async def test_evaluate_pair_parses_supersedes():
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    fake = _mock_haiku(
        '{"relationship": "supersedes", "confidence": 0.92, '
        '"reasoning": "new atom marks the planned task as complete"}'
    )
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake)

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="Zulip integration is complete and in daily use",
            new_type="episodic",
            existing_text="Zulip integration is a planned future task",
            existing_type="episodic",
            existing_age_days=30,
        )

    assert result["relationship"] == "supersedes"
    assert result["confidence"] == 0.92
    assert "complete" in result["reasoning"]
    assert result["usage"]["input_tokens"] == 100


@pytest.mark.asyncio
async def test_evaluate_pair_parses_tension_with():
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    fake = _mock_haiku(
        '{"relationship": "tension_with", "confidence": 0.78, '
        '"reasoning": "anomaly does not invalidate Newtonian framework"}'
    )
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake)

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="Mercury's perihelion precesses anomalously",
            new_type="semantic",
            existing_text="Newtonian gravity accurately predicts orbits",
            existing_type="semantic",
            existing_age_days=2,
        )

    assert result["relationship"] == "tension_with"
    assert result["confidence"] == 0.78


@pytest.mark.asyncio
async def test_evaluate_pair_strips_markdown_fences():
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    fake = _mock_haiku(
        '```json\n{"relationship": "narrows", "confidence": 0.70, '
        '"reasoning": "qualifies the original"}\n```'
    )
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake)

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="Tom uses Zulip for ops, Mattermost for personal",
            new_type="semantic",
            existing_text="Tom uses Mattermost",
            existing_type="semantic",
            existing_age_days=1,
        )

    assert result["relationship"] == "narrows"
    assert result["confidence"] == 0.70


@pytest.mark.asyncio
async def test_evaluate_pair_returns_none_on_unknown_relationship():
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    fake = _mock_haiku('{"relationship": "weird_made_up", "confidence": 0.9}')
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake)

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="x", new_type="semantic",
            existing_text="y", existing_type="semantic",
            existing_age_days=1,
        )
    assert result is None


@pytest.mark.asyncio
async def test_evaluate_pair_retries_once_then_returns_none():
    """Spec §4: single retry on transient error, then give up."""
    from mnemo.server.services.lifecycle_service import _evaluate_pair

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=RuntimeError("transient"))

    with patch("mnemo.server.services.lifecycle_service._get_client", return_value=mock_client):
        result = await _evaluate_pair(
            new_text="x", new_type="semantic",
            existing_text="y", existing_type="semantic",
            existing_age_days=1,
        )

    assert result is None
    assert mock_client.messages.create.await_count == 2  # initial + 1 retry
```

- [ ] **Step 2: Run — should fail**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_lifecycle_service.py -v`
Expected: 5 new failures (no `_evaluate_pair`).

- [ ] **Step 3: Implement `_evaluate_pair`**

Append to `mnemo/server/services/lifecycle_service.py`:

```python
import asyncio
import json
from functools import lru_cache

from anthropic import AsyncAnthropic

MODEL = "claude-haiku-4-5-20251001"

LIFECYCLE_SYSTEM_PROMPT = """You are evaluating the relationship between a newly stored memory atom and an existing atom about a similar topic.

Classify the relationship. Respond with JSON only, no prose, no markdown:
{"relationship": "supersedes" | "tension_with" | "narrows" | "independent", "confidence": 0.0-1.0, "reasoning": "<one sentence>"}

Definitions:
- "supersedes": the new atom replaces the existing one. Use this for state changes, corrections, and preference updates where the existing atom is now historically accurate but no longer current. Examples: "X is planned" -> "X is done"; "Tom prefers A" -> "Tom now prefers B"; "Score is 76.1%" -> "Score was actually 82.1%; 76.1% was an earlier result".

- "tension_with": both atoms remain true and active, but together they identify an unresolved discrepancy or anomaly worth surfacing. Use this when the new atom is *evidence against* or *in tension with* the existing one without directly invalidating it. Examples: "Newtonian gravity works" + "Mercury's perihelion precesses anomalously"; "Mnemo achieves 82.1% on LoCoMo" + "Hindsight achieves 91.4% on LongMemEval"; "Strategy X has worked historically" + "Strategy X failed in Q4".

- "narrows": the new atom qualifies or refines the existing one without invalidating it. Both should remain visible together. Examples: "Tom uses Mattermost" -> "Tom uses Zulip for ops, Mattermost for personal"; "Mnemo runs on Postgres" -> "Mnemo runs on Postgres 16 with pgvector".

- "independent": same topic, no logical relationship between them.

Important guardrail:
If the existing atom is a SEMANTIC claim about how the world works (rather than an EPISODIC fact about a state, event, or measurement), strongly prefer "tension_with" over "supersedes" unless the new atom explicitly corrects or invalidates the existing claim with overwhelming evidence. Semantic claims are rarely retired by single new observations; they accumulate evidence and shift through "tension_with" relationships."""

_VALID_RELATIONSHIPS = {"supersedes", "tension_with", "narrows", "independent"}


@lru_cache(maxsize=1)
def _get_client() -> AsyncAnthropic:
    """Singleton Anthropic client. Tests patch this same way as llm_decomposer:
    `patch('mnemo.server.services.lifecycle_service._get_client', ...)`."""
    return AsyncAnthropic()


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()


async def _evaluate_pair(
    new_text: str,
    new_type: str,
    existing_text: str,
    existing_type: str,
    existing_age_days: int,
) -> dict | None:
    """Call Haiku to classify the (existing, new) pair. One retry on transient
    error per spec §4. Returns None on permanent failure."""
    user_prompt = (
        f"EXISTING ATOM (stored {existing_age_days} days ago, type: {existing_type}):\n"
        f'"{existing_text}"\n\n'
        f"NEW ATOM (just stored, type: {new_type}):\n"
        f'"{new_text}"'
    )
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            client = _get_client()
            response = await asyncio.wait_for(
                client.messages.create(
                    model=MODEL,
                    max_tokens=256,
                    system=[{
                        "type": "text",
                        "text": LIFECYCLE_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_prompt}],
                ),
                timeout=settings.lifecycle_llm_timeout_seconds,
            )
            raw = _strip_fences(response.content[0].text)
            parsed = json.loads(raw)
            rel = parsed.get("relationship")
            if rel not in _VALID_RELATIONSHIPS:
                return None
            return {
                "relationship": rel,
                "confidence": float(parsed.get("confidence", 0.0)),
                "reasoning": str(parsed.get("reasoning", ""))[:500],
                "usage": {
                    "model": response.model,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
            }
        except json.JSONDecodeError:
            return None
        except Exception as e:
            last_err = e
            continue
    logger.warning("lifecycle LLM call failed after retry: %s", last_err)
    return None
```

- [ ] **Step 4: Run — should pass**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_lifecycle_service.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add mnemo/server/services/lifecycle_service.py tests/test_lifecycle_service.py
git commit -m "$(cat <<'EOF'
feat(lifecycle): Haiku 4-way classifier with single transient-error retry

Structured-JSON classification of (existing, new) atom pairs into
supersedes / tension_with / narrows / independent. System prompt carries
the episodic/semantic guardrail. 5s timeout (configurable), one retry on
transient error per spec §4.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: Orchestrator + idempotency pre-check + structured log line + DLQ writer

**Files:**
- Modify: `mnemo/server/services/lifecycle_service.py`
- Modify: `tests/test_lifecycle_service.py`

- [ ] **Step 1: Append failing tests for the orchestrator**

Append to `tests/test_lifecycle_service.py`:

```python
def _wrap(value):
    """Sync value -> awaitable for use as a side_effect on a regular Mock."""
    async def _coro():
        return value
    return _coro()


def _eval_returning(payload):
    return lambda **kw: _wrap(payload)


# ── Edge writes ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_writes_supersedes_at_or_above_threshold(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        old_id, _ = await _insert(conn, agent_id, "Zulip integration is a planned future task", "episodic")
        new_id, _ = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic")

        payload = {
            "relationship": "supersedes",
            "confidence": 0.85,
            "reasoning": "marks planned task as complete",
            "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1},
        }
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_returning(payload)):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)

        assert n == 1
        edge = await conn.fetchrow(
            "SELECT edge_type, weight, metadata FROM edges WHERE source_id=$1 AND target_id=$2",
            new_id, old_id,
        )
        assert edge["edge_type"] == "supersedes"
        assert edge["weight"] == pytest.approx(0.85, abs=1e-6)
        meta = json.loads(edge["metadata"])
        assert meta["detector"] == "auto_lifecycle_v1"
        assert meta["reasoning"].startswith("marks")
        assert "cosine_at_detection" in meta


@pytest.mark.asyncio
async def test_writes_tension_with_at_or_above_threshold(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        old_id, _ = await _insert(conn, agent_id, "Newtonian gravity accurately predicts orbits", "semantic")
        new_id, _ = await _insert(conn, agent_id, "Mercury's perihelion precesses anomalously", "semantic")

        payload = {
            "relationship": "tension_with",
            "confidence": 0.70,
            "reasoning": "anomaly without invalidation",
            "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1},
        }
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_returning(payload)):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)

        assert n == 1
        edge = await conn.fetchrow(
            "SELECT edge_type FROM edges WHERE source_id=$1 AND target_id=$2",
            new_id, old_id,
        )
        assert edge["edge_type"] == "tension_with"


@pytest.mark.asyncio
async def test_writes_narrows_at_or_above_threshold(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        await _insert(conn, agent_id, "Tom uses Mattermost for all communication", "semantic")
        new_id, _ = await _insert(conn, agent_id, "Tom uses Zulip for ops, Mattermost for personal", "semantic")

        payload = {
            "relationship": "narrows",
            "confidence": 0.70,
            "reasoning": "qualifies",
            "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1},
        }
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_returning(payload)):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)

        assert n == 1
        n_narrows = await conn.fetchval(
            "SELECT COUNT(*) FROM edges WHERE edge_type = 'narrows'"
        )
        assert n_narrows == 1


# ── Threshold gating ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_supersedes_below_threshold_no_edge(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        await _insert(conn, agent_id, "Zulip integration is a planned future task", "episodic")
        new_id, _ = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic")

        payload = {
            "relationship": "supersedes", "confidence": 0.70,  # below 0.75
            "reasoning": "low conf",
            "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1},
        }
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_returning(payload)):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)
        assert n == 0
        assert await conn.fetchval("SELECT COUNT(*) FROM edges") == 0


@pytest.mark.asyncio
async def test_independent_no_edge(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        await _insert(conn, agent_id, "Tom is co-founder of Inforge LLC", "semantic")
        new_id, _ = await _insert(conn, agent_id, "Inforge LLC was incorporated in Delaware in March 2023", "semantic")

        payload = {
            "relationship": "independent", "confidence": 0.95,
            "reasoning": "facets",
            "usage": {"model": "x", "input_tokens": 1, "output_tokens": 1},
        }
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_returning(payload)):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)
        assert n == 0


# ── No candidates ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_candidates_skips_llm(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        new_id, _ = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic")
        called = {"n": 0}
        async def _spy(**kw):
            called["n"] += 1
            return None
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_spy):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)
        assert n == 0
        assert called["n"] == 0


# ── Idempotency: no competing edges ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_pair_with_existing_lifecycle_edge_skips_llm(pool, agent_with_address):
    """Per spec: no competing edges. If any lifecycle edge already exists for
    a pair (either direction), skip evaluation entirely — do not call the LLM."""
    from mnemo.server.services import lifecycle_service, atom_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        old_id, _ = await _insert(conn, agent_id, "Zulip integration is a planned future task", "episodic")
        new_id, _ = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic")
        # Pre-existing tension_with edge between the pair (reverse direction).
        await atom_service.create_edge(
            conn=conn, source_id=old_id, target_id=new_id,
            edge_type="tension_with", weight=0.7, metadata={"detector": "manual"},
        )
        called = {"n": 0}
        async def _spy(**kw):
            called["n"] += 1
            return None
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_spy):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)
        assert n == 0
        assert called["n"] == 0


# ── DLQ ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_permanent_failure_writes_dlq(pool, agent_with_address):
    from mnemo.server.services import lifecycle_service

    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        cand_id, _ = await _insert(conn, agent_id, "Zulip integration is a planned future task", "episodic")
        new_id, _ = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic")

        # _evaluate_pair returns None (permanent failure post-retry).
        async def _eval_none(**kw):
            return None
        with patch.object(lifecycle_service, "_evaluate_pair", side_effect=_eval_none):
            n = await lifecycle_service.detect_lifecycle_relationships(conn, agent_id, new_id)

        assert n == 0
        dlq = await conn.fetch(
            "SELECT new_atom_id, candidate_id, agent_id FROM lifecycle_dlq"
        )
        assert len(dlq) >= 1
        assert dlq[0]["new_atom_id"] == new_id
        assert dlq[0]["candidate_id"] == cand_id
```

Add `import json` to the top of the test file if not already present.

- [ ] **Step 2: Run — should fail**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_lifecycle_service.py -v`
Expected: many new failures — `detect_lifecycle_relationships` doesn't exist yet.

- [ ] **Step 3: Implement orchestrator + helpers**

Append to `mnemo/server/services/lifecycle_service.py`:

```python
import time
from datetime import datetime, timezone

from . import atom_service

DETECTOR_VERSION = "auto_lifecycle_v1"

_THRESHOLDS: dict[str, str] = {
    "supersedes": "supersedes_threshold",
    "tension_with": "tension_threshold",
    "narrows": "narrows_threshold",
}


async def _pair_has_lifecycle_edge(
    conn: asyncpg.Connection,
    a_id: UUID,
    b_id: UUID,
) -> bool:
    """Return True if any edge of any lifecycle type connects this pair, in
    either direction. Spec §Edge creation: no competing edges."""
    row = await conn.fetchval(
        """
        SELECT 1 FROM edges
        WHERE edge_type IN ('supersedes', 'tension_with', 'narrows')
          AND (
            (source_id = $1 AND target_id = $2)
            OR (source_id = $2 AND target_id = $1)
          )
        LIMIT 1
        """,
        a_id, b_id,
    )
    return row is not None


async def _record_dlq(
    conn: asyncpg.Connection,
    new_atom_id: UUID,
    candidate_id: UUID | None,
    agent_id: UUID,
    error: str,
) -> None:
    try:
        await conn.execute(
            """
            INSERT INTO lifecycle_dlq (new_atom_id, candidate_id, agent_id, error)
            VALUES ($1, $2, $3, $4)
            """,
            new_atom_id, candidate_id, agent_id, error[:1000],
        )
    except Exception:
        logger.warning("failed to record lifecycle_dlq row", exc_info=True)


async def detect_lifecycle_relationships(
    conn: asyncpg.Connection,
    agent_id: UUID,
    new_atom_id: UUID,
) -> int:
    """For one newly-inserted atom, run candidate query + LLM eval per
    candidate and write the appropriate edge type when the model is confident
    enough. Permanent LLM failures land in lifecycle_dlq.

    Returns the count of edges written by this call. Never raises."""
    new_row = await conn.fetchrow(
        "SELECT id, text_content, atom_type, embedding, created_at FROM atoms WHERE id = $1",
        new_atom_id,
    )
    if new_row is None:
        return 0

    candidates = await _get_candidates(conn, agent_id, new_atom_id, new_row["embedding"])
    if not candidates:
        return 0

    edges_written = 0
    for cand in candidates:
        # Idempotency / no-competing-edges: skip if any lifecycle edge exists
        # for this pair already (either direction). Saves an LLM call too.
        if await _pair_has_lifecycle_edge(conn, new_atom_id, cand["id"]):
            continue

        t0 = time.monotonic()
        existing_age_days = max(
            0,
            int((datetime.now(timezone.utc) - cand["created_at"]).total_seconds() / 86400),
        )
        result = await _evaluate_pair(
            new_text=new_row["text_content"],
            new_type=new_row["atom_type"],
            existing_text=cand["text_content"],
            existing_type=cand["atom_type"],
            existing_age_days=existing_age_days,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        if result is None:
            await _record_dlq(conn, new_atom_id, cand["id"], agent_id, "lifecycle LLM permanent failure")
            logger.warning(
                "lifecycle_check",
                extra={
                    "event": "lifecycle_check",
                    "new_atom_id": str(new_atom_id),
                    "candidate_atom_id": str(cand["id"]),
                    "agent_id": str(agent_id),
                    "cosine": cand["similarity"],
                    "edge_created": False,
                    "latency_ms": latency_ms,
                    "dlq": True,
                },
            )
            continue

        rel = result["relationship"]
        edge_type: str | None = None
        if rel in _THRESHOLDS:
            threshold = getattr(settings, _THRESHOLDS[rel])
            if result["confidence"] >= threshold:
                edge_type = rel

        edge_created = False
        if edge_type is not None:
            try:
                edge = await atom_service.create_edge(
                    conn=conn,
                    source_id=new_atom_id,
                    target_id=cand["id"],
                    edge_type=edge_type,
                    weight=float(result["confidence"]),
                    metadata={
                        "reasoning": result["reasoning"],
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                        "detector": DETECTOR_VERSION,
                        "cosine_at_detection": cand["similarity"],
                    },
                )
                if edge is not None:
                    edges_written += 1
                    edge_created = True
            except Exception:
                logger.warning("lifecycle edge write failed", exc_info=True)

        usage = result.get("usage") or {}
        logger.info(
            "lifecycle_check",
            extra={
                "event": "lifecycle_check",
                "new_atom_id": str(new_atom_id),
                "candidate_atom_id": str(cand["id"]),
                "agent_id": str(agent_id),
                "new_atom_type": new_row["atom_type"],
                "existing_atom_type": cand["atom_type"],
                "cosine": cand["similarity"],
                "llm_relationship": rel,
                "llm_confidence": result["confidence"],
                "llm_reasoning": result.get("reasoning"),
                "edge_created": edge_created,
                "edge_type": edge_type if edge_created else None,
                "latency_ms": latency_ms,
                "haiku_input_tokens": usage.get("input_tokens"),
                "haiku_output_tokens": usage.get("output_tokens"),
            },
        )

    return edges_written
```

- [ ] **Step 4: Run — should pass**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_lifecycle_service.py -v`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add mnemo/server/services/lifecycle_service.py tests/test_lifecycle_service.py
git commit -m "$(cat <<'EOF'
feat(lifecycle): orchestrator + idempotency + DLQ + structured log

detect_lifecycle_relationships() runs candidate query, skips pairs with any
existing lifecycle edge (no competing edges per spec), calls Haiku, and
writes the matching edge type when confidence clears its asymmetric
threshold. One event=lifecycle_check JSON log line per check. Permanent
LLM failures land in lifecycle_dlq.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 14: Wire `detect_lifecycle_relationships` into `store_background` (feature-flagged)

**Files:**
- Modify: `mnemo/server/services/atom_service.py:617-680`
- Test: `tests/test_lifecycle_service.py`

- [ ] **Step 1: Append integration test**

Append to `tests/test_lifecycle_service.py`:

```python
@pytest.mark.asyncio
async def test_store_background_invokes_lifecycle_when_enabled(pool, agent_with_address):
    from uuid import uuid4
    from mnemo.server.services import atom_service, lifecycle_service

    agent_id = agent_with_address["id"]
    captured = []
    async def _spy(conn, agent, new_id):
        captured.append(new_id)
        return 0

    with patch.object(lifecycle_service, "detect_lifecycle_relationships", side_effect=_spy):
        store_id = uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO store_jobs (store_id, agent_id) VALUES ($1, $2)",
                store_id, agent_id,
            )
        await atom_service.store_background(
            pool=pool, store_id=store_id, agent_id=agent_id,
            text="The sky over Boston was clear on April 26 2026.", domain_tags=["t"],
        )

    assert len(captured) >= 1


@pytest.mark.asyncio
async def test_store_background_skips_lifecycle_when_disabled(pool, agent_with_address, monkeypatch):
    from uuid import uuid4
    from mnemo.server.config import settings
    from mnemo.server.services import atom_service, lifecycle_service

    monkeypatch.setattr(settings, "lifecycle_detection_enabled", False)

    agent_id = agent_with_address["id"]
    called = {"n": 0}
    async def _spy(conn, agent, new_id):
        called["n"] += 1
        return 0

    with patch.object(lifecycle_service, "detect_lifecycle_relationships", side_effect=_spy):
        store_id = uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO store_jobs (store_id, agent_id) VALUES ($1, $2)",
                store_id, agent_id,
            )
        await atom_service.store_background(
            pool=pool, store_id=store_id, agent_id=agent_id,
            text="Random observation about the weather.", domain_tags=["t"],
        )

    assert called["n"] == 0
```

- [ ] **Step 2: Run — should fail**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_lifecycle_service.py::test_store_background_invokes_lifecycle_when_enabled -v`
Expected: AssertionError.

- [ ] **Step 3: Wire the hook**

Edit `mnemo/server/services/atom_service.py`. In `store_background`, replace the body of the outer `try:` block so that, after the `UPDATE store_jobs SET status = 'complete' …` write, it reads:

```python
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE store_jobs SET status = 'decomposing' WHERE store_id = $1",
                store_id,
            )
            async with conn.transaction():
                result = await store_from_text(
                    conn, agent_id, text, domain_tags,
                    store_id=store_id, operator_id=operator_id,
                    remembered_on=remembered_on,
                )
            await conn.execute(
                """
                UPDATE store_jobs
                SET status = 'complete', atoms_created = $1, completed_at = now()
                WHERE store_id = $2
                """,
                result["atoms_created"],
                store_id,
            )
            # Post-store: lifecycle relationship detection. Best-effort,
            # never raises, gated by feature flag.
            from ..config import settings as _settings
            if _settings.lifecycle_detection_enabled:
                from .lifecycle_service import detect_lifecycle_relationships
                for new_atom_id in result.get("new_atom_ids", []):
                    try:
                        await detect_lifecycle_relationships(conn, agent_id, new_atom_id)
                    except Exception:
                        logger.warning(
                            "detect_lifecycle_relationships failed for atom %s", new_atom_id,
                            exc_info=True,
                        )
```

- [ ] **Step 4: Run — should pass**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_lifecycle_service.py -v`
Expected: 16 passed.

- [ ] **Step 5: Run the full default suite**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest -x -q`
Expected: all passed (eval excluded).

- [ ] **Step 6: Commit**

```bash
git add mnemo/server/services/atom_service.py tests/test_lifecycle_service.py
git commit -m "$(cat <<'EOF'
feat(lifecycle): hook detect_lifecycle_relationships into store_background

Feature-flagged via MNEMO_LIFECYCLE_DETECTION_ENABLED (default false in
prod, true in tests via conftest). After store_from_text's transaction
commits, iterate result['new_atom_ids'] and run detection on each.
Per-atom errors are logged and swallowed; never blocks the store.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: Recall path attaches `lifecycle_edges` metadata

**Files:**
- Modify: `mnemo/server/services/atom_service.py` (around the end of `retrieve()` near line 900)
- Test: `tests/test_recall_lifecycle_metadata.py`

- [ ] **Step 1: Write a failing integration test**

Create `tests/test_recall_lifecycle_metadata.py`:

```python
"""Recall response carries lifecycle_edges metadata for tension_with / narrows
edges. Supersedes edges remain hidden by _filter_superseded."""
import pytest

from mnemo.server.services.atom_service import _insert_atom, create_edge, retrieve
from mnemo.server.decomposer import DecomposedAtom
from mnemo.server.embeddings import encode


async def _insert(conn, agent_id, text, atom_type="semantic"):
    emb = await encode(text)
    row = await _insert_atom(
        conn, agent_id,
        DecomposedAtom(text=text, atom_type=atom_type,
                       confidence_alpha=4.0, confidence_beta=2.0),
        emb, ["t"], "direct_experience",
    )
    return row["id"]


@pytest.mark.asyncio
async def test_recall_attaches_tension_with_edges(pool, agent_with_address):
    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        a_id = await _insert(conn, agent_id, "Newtonian gravity accurately predicts orbits")
        b_id = await _insert(conn, agent_id, "Mercury's perihelion precesses anomalously")
        await create_edge(
            conn=conn, source_id=b_id, target_id=a_id,
            edge_type="tension_with", weight=0.78,
            metadata={"reasoning": "anomaly", "detector": "auto_lifecycle_v1"},
        )

        result = await retrieve(
            conn=conn, agent_id=agent_id, query="Newtonian gravity validity",
            domain_tags=None, min_confidence=0.0, min_similarity=0.0,
            max_results=10, expand_graph=False, expansion_depth=0,
            include_superseded=False, similarity_drop_threshold=None,
            verbosity="standard", max_content_chars=None, max_total_tokens=None,
        )

    by_id = {a["id"]: a for a in result["atoms"]}
    assert a_id in by_id and b_id in by_id

    a_edges = by_id[a_id].get("lifecycle_edges") or []
    b_edges = by_id[b_id].get("lifecycle_edges") or []
    # Both endpoints expose the tension; the relationship is symmetric in surface.
    assert any(
        e["related_atom_id"] == b_id and e["relationship"] == "tension_with"
        for e in a_edges
    ), f"a_edges: {a_edges}"
    assert any(
        e["related_atom_id"] == a_id and e["relationship"] == "tension_with"
        for e in b_edges
    ), f"b_edges: {b_edges}"


@pytest.mark.asyncio
async def test_recall_does_not_surface_supersedes_in_lifecycle_edges(pool, agent_with_address):
    """supersedes is filtered server-side; the surviving atom doesn't carry
    a lifecycle_edges entry pointing at the retired atom."""
    agent_id = agent_with_address["id"]
    async with pool.acquire() as conn:
        old_id = await _insert(conn, agent_id, "Zulip integration is a planned future task", "episodic")
        new_id = await _insert(conn, agent_id, "Zulip integration is complete and in daily use", "episodic")
        await create_edge(
            conn=conn, source_id=new_id, target_id=old_id,
            edge_type="supersedes", weight=0.9,
            metadata={"reasoning": "x", "detector": "auto_lifecycle_v1"},
        )

        result = await retrieve(
            conn=conn, agent_id=agent_id, query="Zulip integration status",
            domain_tags=None, min_confidence=0.0, min_similarity=0.0,
            max_results=10, expand_graph=False, expansion_depth=0,
            include_superseded=False, similarity_drop_threshold=None,
            verbosity="standard", max_content_chars=None, max_total_tokens=None,
        )

    ids = [a["id"] for a in result["atoms"]]
    assert old_id not in ids
    new_atom = next(a for a in result["atoms"] if a["id"] == new_id)
    assert (new_atom.get("lifecycle_edges") or []) == []
```

- [ ] **Step 2: Run — should fail**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_recall_lifecycle_metadata.py -v`
Expected: AssertionError (lifecycle_edges is None / missing).

- [ ] **Step 3: Implement the recall extension**

Edit `mnemo/server/services/atom_service.py`. Locate the end of `retrieve()` around line 900-906 (the final return building `all_atoms`). Replace the final block so it reads:

```python
    primary_responses = _apply_verbosity(primary_responses, verbosity, max_content_chars)
    expanded_responses = _apply_verbosity(expanded_responses, verbosity, max_content_chars)

    all_atoms = primary_responses + expanded_responses
    await _attach_lifecycle_edges(conn, all_atoms)
    return {
        "atoms": all_atoms,
        "total_retrieved": len(all_atoms),
    }
```

Then add the helper just below `_filter_superseded` (after line 928):

```python
async def _attach_lifecycle_edges(
    conn: asyncpg.Connection,
    atoms: list[dict],
) -> None:
    """Mutates `atoms` in-place: each atom gains a `lifecycle_edges` list with
    any tension_with / narrows edges connecting it to other atoms. supersedes
    is intentionally excluded — those targets are filtered upstream by
    _filter_superseded and we don't surface the relationship metadata."""
    if not atoms:
        return
    atom_ids = [a["id"] for a in atoms]
    rows = await conn.fetch(
        """
        SELECT e.source_id, e.target_id, e.edge_type, e.weight,
               e.metadata->>'reasoning' AS reasoning
        FROM edges e
        JOIN atoms src ON src.id = e.source_id
        JOIN atoms tgt ON tgt.id = e.target_id
        WHERE e.edge_type IN ('tension_with', 'narrows')
          AND src.is_active = true
          AND tgt.is_active = true
          AND (e.source_id = ANY($1) OR e.target_id = ANY($1))
        """,
        atom_ids,
    )
    by_atom: dict = {aid: [] for aid in atom_ids}
    seen: set = set()
    for r in rows:
        src, tgt, et = r["source_id"], r["target_id"], r["edge_type"]
        # surface the edge on whichever endpoint is in the result set.
        # de-dup on (this_atom, related_atom, edge_type).
        if src in by_atom:
            key = (src, tgt, et)
            if key not in seen:
                seen.add(key)
                by_atom[src].append({
                    "related_atom_id": tgt,
                    "relationship": et,
                    "reasoning": r["reasoning"],
                    "weight": float(r["weight"]),
                })
        if tgt in by_atom:
            key = (tgt, src, et)
            if key not in seen:
                seen.add(key)
                by_atom[tgt].append({
                    "related_atom_id": src,
                    "relationship": et,
                    "reasoning": r["reasoning"],
                    "weight": float(r["weight"]),
                })
    for atom in atoms:
        atom["lifecycle_edges"] = by_atom.get(atom["id"], [])
```

- [ ] **Step 4: Run — should pass**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest tests/test_recall_lifecycle_metadata.py -v`
Expected: 2 passed.

- [ ] **Step 5: Run the full default suite**

Run: `cd /home/tompdavis/mnemo-server && uv run pytest -x -q`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add mnemo/server/services/atom_service.py tests/test_recall_lifecycle_metadata.py
git commit -m "$(cat <<'EOF'
feat(recall): attach lifecycle_edges metadata to recall results

Each returned atom carries a lifecycle_edges list of {related_atom_id,
relationship, reasoning, weight} for any active tension_with or narrows
edge it participates in. supersedes is filtered upstream and never
surfaces here.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 16: Remove `xfail` markers and verify the eval suite passes

**Files:**
- Modify: `tests/eval/test_lifecycle_eval.py`

- [ ] **Step 1: Remove the xfail marker**

Edit `tests/eval/test_lifecycle_eval.py`. Replace the `pytestmark = [...]` block with:

```python
pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="eval requires ANTHROPIC_API_KEY",
    ),
]
```

- [ ] **Step 2: Run eval against live Haiku**

Run (requires `ANTHROPIC_API_KEY`):
`cd /home/tompdavis/mnemo-server && uv run pytest tests/eval/ -m eval -v`
Expected: 9 passed. Allow up to ~3 minutes.

- [ ] **Step 3: If any case fails, debug via the structured log lines**

Each `lifecycle_check` log line (event=`lifecycle_check`) carries `cosine`, `new_atom_type`, `existing_atom_type`, `llm_relationship`, `llm_confidence`, `llm_reasoning`. Common fixes:
- Case 7 (semantic tension) failing as `supersedes`: tighten the guardrail copy in `LIFECYCLE_SYSTEM_PROMPT`, or raise `MNEMO_SUPERSEDES_THRESHOLD` to 0.80 to require more LLM confidence on retire-the-old verdicts.
- Cases 1/2/4/9 failing as `independent`: cosine likely below `lifecycle_band_low`. Check the log; if pairs are landing at 0.45-0.50 you may want `lifecycle_band_low=0.45`.
- Case 3 (dedup) firing a `narrows` or `tension_with`: dedup should have merged before lifecycle ran. Inspect the store path; the duplicate should land in `_check_duplicate` and the second store should produce 0 new_atom_ids.

- [ ] **Step 4: Commit**

```bash
git add tests/eval/test_lifecycle_eval.py
git commit -m "$(cat <<'EOF'
test(eval): remove xfail markers — lifecycle service now passes

All 9 canonical cases pass against live Haiku. Eval moves from
forcing-function to regression-guard.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 17: Open PR 3

- [ ] **Step 1: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --title "feat(lifecycle): auto-detect supersedes / tension_with / narrows" --body "$(cat <<'EOF'
## Summary
- New `mnemo/server/services/lifecycle_service.py`. Four-way classifier (supersedes / tension_with / narrows / independent) running post-store on each newly-inserted atom against ANN candidates in cosine band [0.50, 0.90).
- Asymmetric thresholds: supersedes 0.75, tension/narrows 0.65 — destructive verdicts cost more confidence.
- "No competing edges" idempotency: if any lifecycle edge already connects a pair (either direction), skip evaluation entirely (saves an LLM call too).
- Migration 006 extends `edge_type` allowlist with `tension_with` + `narrows`, adds nullable `metadata` JSONB to `edges`, and adds a `lifecycle_dlq` table for permanent Haiku failures.
- Recall response now carries an optional `lifecycle_edges` list per atom for `tension_with` / `narrows` participation. `supersedes` remains filtered server-side by `_filter_superseded`.
- Feature-flagged via `MNEMO_LIFECYCLE_DETECTION_ENABLED` (default false in production; conftest sets it true for tests).
- Per-check observability: one `event=lifecycle_check` JSON log line per candidate via the formatter landed in PR 1.

## Product positioning
This change distinguishes *state changes* (supersedes, retire-on-recall) from *evidential tensions* (tension_with, surface-with-context). Most memory systems collapse both into "latest wins"; Mnemo preserves the structure of what's actually known — including what doesn't fit.

## Risk surface
- Cost: ~$0.0001/store estimated overhead. The "no competing edges" pre-check materially reduces re-runs.
- False positives on `supersedes` are destructive (silently retires a memory). Asymmetric threshold + the episodic/semantic guardrail in the prompt mitigate. Sample 100 production cases pre-mnemo-net rollout.
- Recall API surface change: new `lifecycle_edges` field is optional; existing consumers unaffected.

## Test plan
- [x] `pytest tests/test_lifecycle_service.py -v` (16 passed; DB-integration with mocked LLM)
- [x] `pytest tests/test_recall_lifecycle_metadata.py -v` (2 passed)
- [x] `pytest -x -q` (full default suite green)
- [x] `pytest -m eval -v` (9 passed against live Haiku)
- [ ] Deploy to inforge-ops with the flag on; sample lifecycle_check log lines for verdict distribution and FP rate

## Follow-ups (deferred, per spec open questions)
- Cross-agent detection (`tension_with` likely safer to enable cross-agent first).
- Confidence transfer for atoms B that supersede A.
- Tension-cluster scoring (an atom with multiple tension_with edges is "contested").
- Retroactive sweep over the existing vault.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Checklist

- **Spec coverage:**
  - §1 Models — Tasks 7, 8 (migration + Literal extension + LifecycleEdge model) ✓
  - §2 Service layer — Tasks 11, 12, 13 (`lifecycle_service.py` with `detect_lifecycle_relationships`) ✓
  - §3 Async hook — Task 14 (feature-flagged hook in `store_background`); DLQ → Tasks 7, 13 ✓ ; queue-depth metric: deferred to a follow-up admin endpoint (rationale: row count of `lifecycle_dlq` is queryable directly via SQL; spec calls for "metric" but shipping a Prometheus exporter is out of proportion to v1 scope. Flag in PR description.)
  - §4 LLM client — Task 12 (Haiku, JSON, 5s timeout, single retry, then DLQ via Task 13) ✓
  - §5 Eval set — Tasks 4, 5, 16 (9 cases including the episodic/semantic guardrail counterpoint) ✓
  - §6a Central logging — Tasks 1, 2 ✓
  - §6b Lifecycle log lines — Task 13 (all spec fields including `new_atom_type`, `existing_atom_type`, `llm_reasoning`, `edge_type`) ✓
  - §6c Smoke test — Task 1 step 1 (`test_configure_logging_routes_records_through_json_formatter`) ✓
  - Recall behavior (`tension_with` + `narrows` surfaced as `lifecycle_edges`) — Task 15 ✓
  - Idempotency / no competing edges — Task 13 (`_pair_has_lifecycle_edge` pre-check) ✓
  - Feature flag `MNEMO_LIFECYCLE_DETECTION_ENABLED` — Task 11, Task 14 ✓
- **Type/name consistency:**
  - `detect_lifecycle_relationships(conn, agent_id, new_atom_id) -> int` — same signature in Tasks 13, 14, and tests ✓
  - `_get_candidates`, `_evaluate_pair`, `_pair_has_lifecycle_edge`, `_record_dlq` — names consistent across Tasks 11–13 ✓
  - `result["new_atom_ids"]` — produced in Task 10, consumed in Task 14 ✓
  - `LifecycleEdge` model fields (`related_atom_id`, `relationship`, `reasoning`, `weight`) match the dicts produced in Task 15 ✓
  - Settings names and env vars align (`MNEMO_LIFECYCLE_DETECTION_ENABLED` ↔ `lifecycle_detection_enabled`; `MNEMO_SUPERSEDES_THRESHOLD` ↔ `supersedes_threshold`; etc.) ✓
- **Placeholder scan:** no TBD / TODO; every code step shows complete code.
- **Open spec deviation called out:** `lifecycle_queue_depth` Prometheus-style metric is deferred; the DLQ table is queryable via SQL in v1. PR description flags this.

---

## Rollout (post-merge)

1. PR 1 lands → JSON logs in dev.
2. PR 2 lands → CI reports 9 xfailed under `-m eval`.
3. PR 3 lands → eval passes; deploy to `inforge-ops` with `MNEMO_LIFECYCLE_DETECTION_ENABLED=true` (internal only). Watch the `event=lifecycle_check` stream for verdict distribution. Calibration target: 30–60% `independent` rate inside the cosine band.
4. Hand-label ~100 production cases. Verify `supersedes` FP rate < 3% and `tension_with` / `narrows` FP rate < 10% per spec success criteria.
5. Promote to `mnemo-net` once cost overhead < $0.0002/store average and FP rates clear.

## Open questions deferred to v1.1 (per spec)

- Cross-agent relationship detection (`tension_with` likely the safer first opt-in).
- Confidence transfer when B supersedes A.
- Tension-cluster scoring.
- Retroactive sweep over the existing vault (~$50 estimated).
- Prompt-drift discipline: every new category should land first as a logged-but-not-acted verdict until eval data justifies promotion.
