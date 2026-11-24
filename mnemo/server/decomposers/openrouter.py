"""OpenRouter decomposer — bake-off / eval use only (spec rule 4).

Refuses to construct unless MNEMO_ALLOW_NON_ANTHROPIC_DECOMPOSER=true and
the database is mnemo_eval or a test database (enforced in Settings), so no
production memory text can reach a non-Anthropic provider.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from ..config import settings
from .base import DecomposeRequest, DecomposeResult, DecomposerError, parse_items, prompt_module, version_string
from .prompt_v2 import TOOL_NAME

URL = "https://openrouter.ai/api/v1/chat/completions"
CALL_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 2
_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


def non_anthropic_allowed(database_url: str) -> bool:
    return "mnemo_eval" in database_url or "test" in database_url


class OpenRouterDecomposer:
    provider = "openrouter"

    def __init__(self, model: str, api_key: str | None = None):
        if not (settings.allow_non_anthropic_decomposer and non_anthropic_allowed(settings.database_url)):
            raise DecomposerError("openrouter decomposer is not allowed against this database")
        self.model = model
        self._key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self._key:
            raise DecomposerError("OPENROUTER_API_KEY is not set")

    async def decompose(self, req: DecomposeRequest) -> DecomposeResult:
        prompt = prompt_module()
        body = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": [
                {"role": "system", "content": prompt.build_system_prompt(req)},
                {"role": "user", "content": req.text},
            ],
            "tools": [{"type": "function", "function": {
                "name": TOOL_NAME, "description": "Record the memory atoms extracted from the note.",
                "parameters": prompt.RECORD_ATOMS_SCHEMA}}],
            "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
        }
        headers = {"Authorization": f"Bearer {self._key}"}
        data = None
        async with httpx.AsyncClient(timeout=CALL_TIMEOUT_SECONDS) as http:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    resp = await http.post(URL, json=body, headers=headers)
                except httpx.TimeoutException as exc:
                    if attempt == MAX_RETRIES:
                        raise DecomposerError("provider timeout") from exc
                except httpx.TransportError as exc:
                    if attempt == MAX_RETRIES:
                        raise DecomposerError("provider unreachable") from exc
                else:
                    if resp.status_code == 200:
                        data = resp.json()
                        break
                    if resp.status_code not in _RETRY_STATUS or attempt == MAX_RETRIES:
                        reason = "rate limited" if resp.status_code == 429 else f"provider error (HTTP {resp.status_code})"
                        raise DecomposerError(reason)
                await asyncio.sleep(0.5 * 2 ** attempt)
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise DecomposerError("truncated response")
        calls = (choice.get("message") or {}).get("tool_calls") or []
        call = next((c for c in calls if (c.get("function") or {}).get("name") == TOOL_NAME), None)
        if call is None:
            raise DecomposerError("no tool call")
        try:
            payload = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            raise DecomposerError("malformed tool output") from exc
        atoms, warnings = parse_items(payload.get("atoms"))
        u = data.get("usage") or {}
        usage = {"model": data.get("model", self.model), "input_tokens": u.get("prompt_tokens", 0),
                 "output_tokens": u.get("completion_tokens", 0),
                 "cache_creation_input_tokens": None, "cache_read_input_tokens": None}
        return DecomposeResult(atoms=atoms, usage=[usage], warnings=warnings,
                               version=version_string(self.provider, self.model))
