"""OpenRouter Decisions client (alpha API; docs read 2026-11-24).

    POST https://openrouter.ai/api/alpha/decisions
    {"model", "state", "questions": {key: {type, instructions, criteria}}, "provider": {"zdr": true}}

Spec rule 4: every request carries provider {"zdr": true}, and a body without
it is never sent (`_send` refuses). No database connection is involved here;
callers hold none while awaiting `ask` (docs/phase2-design.md §5).

Retries: two per model on 429 / 5xx / 524 / 529 and on the transient 402
(`limit_source == openrouter_in_flight_budget`), honouring Retry-After within
the deadline; then the fallback model; then give up.
"""

from __future__ import annotations

import asyncio
import os
import random
import time

import httpx

from ..config import settings
from .base import DecisionResult

URL = "https://openrouter.ai/api/alpha/decisions"
ZDR_PROVIDER = {"zdr": True}
RETRYABLE = frozenset({429, 500, 502, 503, 504, 524, 529})
MAX_BACKOFF = 4.0

# Tests set this False (conftest): constructing a client without an injected
# transport then fails, so no test can reach the network.
REAL_NETWORK_ALLOWED = True


class ZdrViolation(RuntimeError):
    """A request body without provider {"zdr": true} reached the sender."""


def build_body(model: str, state: str, questions: dict[str, dict]) -> dict:
    """The only place request bodies are built."""
    return {"model": model, "state": state, "questions": questions, "provider": dict(ZDR_PROVIDER)}


def _check_zdr(body: dict) -> None:
    if body.get("provider") != ZDR_PROVIDER:
        raise ZdrViolation("refusing to send a decision request without provider {'zdr': true}")


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("Retry-After")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _transient(resp: httpx.Response) -> bool:
    if resp.status_code in RETRYABLE:
        return True
    if resp.status_code == 402:
        try:
            meta = (resp.json().get("error") or {}).get("metadata") or {}
        except ValueError:
            return False
        return meta.get("limit_source") == "openrouter_in_flight_budget"
    return False


class OpenRouterDecisions:
    def __init__(
        self,
        model: str | None = None,
        fallback_model: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ):
        if transport is None and not REAL_NETWORK_ALLOWED:
            raise RuntimeError("real OpenRouter client constructed in a non-eval test")
        self.models = [m for m in (model or settings.decision_model,
                                   fallback_model if fallback_model is not None else settings.decision_fallback_model) if m]
        self._key = api_key or settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")
        self._transport = transport
        self._timeout = timeout if timeout is not None else settings.decision_request_timeout_seconds
        self._retries = retries if retries is not None else settings.decision_retries

    async def _send(self, http: httpx.AsyncClient, body: dict) -> httpx.Response:
        _check_zdr(body)
        return await http.post(URL, json=body, headers={"Authorization": f"Bearer {self._key}"})

    async def ask(self, state: str, questions: dict[str, dict], *, deadline: float | None = None) -> DecisionResult:
        t0 = time.monotonic()
        res = DecisionResult(answers=None)
        if not self._key:
            res.error = "no OpenRouter key"
            return res
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
                for model in self.models:
                    body = build_body(model, state, questions)
                    for attempt in range(self._retries + 1):
                        if deadline is not None and time.monotonic() >= deadline:
                            res.error = "deadline"
                            return res
                        res.attempts += 1
                        wait = None
                        try:
                            resp = await self._send(http, body)
                        except ZdrViolation:
                            raise
                        except httpx.HTTPError as exc:
                            res.errors.append(f"{model}: {type(exc).__name__}")
                        else:
                            if resp.status_code == 200:
                                data = resp.json()
                                res.answers = data.get("answers") or {}
                                res.model = data.get("model") or model
                                res.cost = (data.get("usage") or {}).get("cost")
                                return res
                            res.errors.append(f"{model}: HTTP {resp.status_code}")
                            if not _transient(resp):
                                break                      # not retryable: next model
                            wait = _retry_after(resp)
                        if attempt < self._retries:
                            delay = wait if wait is not None else min(MAX_BACKOFF, 0.25 * 2 ** attempt) * (0.5 + random.random())
                            if deadline is not None and time.monotonic() + delay >= deadline:
                                res.error = "deadline"
                                return res
                            await asyncio.sleep(delay)
            res.error = res.errors[-1] if res.errors else "no answer"
            return res
        except ZdrViolation:
            raise
        except Exception as exc:  # fail open: never raise into the store path
            res.error = f"client error: {type(exc).__name__}"
            return res
        finally:
            res.latency_ms = round((time.monotonic() - t0) * 1000)
