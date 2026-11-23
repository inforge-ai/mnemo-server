"""Anthropic decomposer: forced tool use, explicit retries, no fallback."""

from __future__ import annotations

import logging
from functools import lru_cache

import anthropic
from anthropic import AsyncAnthropic

from .base import DecomposeRequest, DecomposeResult, DecomposerError, parse_items, version_string
from .prompt_v2 import RECORD_ATOMS_TOOL, TOOL_NAME, build_system_prompt

logger = logging.getLogger(__name__)

MAX_TOKENS = 8192
CALL_TIMEOUT_SECONDS = 60.0


@lru_cache(maxsize=1)
def _get_client() -> AsyncAnthropic:
    """Singleton client. The SDK retries connection errors, 408, 409, 429
    and 5xx with exponential backoff: max_retries=2 gives three attempts."""
    return AsyncAnthropic(max_retries=2, timeout=CALL_TIMEOUT_SECONDS)


class AnthropicDecomposer:
    provider = "anthropic"

    def __init__(self, model: str):
        self.model = model

    async def decompose(self, req: DecomposeRequest) -> DecomposeResult:
        client = _get_client()
        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=build_system_prompt(req),
                messages=[{"role": "user", "content": req.text}],
                tools=[RECORD_ATOMS_TOOL],
                tool_choice={"type": "tool", "name": TOOL_NAME},
            )
        except anthropic.APITimeoutError as exc:
            raise DecomposerError("provider timeout") from exc
        except anthropic.RateLimitError as exc:
            raise DecomposerError("rate limited") from exc
        except anthropic.APIConnectionError as exc:
            raise DecomposerError("provider unreachable") from exc
        except anthropic.APIStatusError as exc:
            raise DecomposerError(f"provider error (HTTP {exc.status_code})") from exc

        if response.stop_reason == "max_tokens":
            raise DecomposerError("truncated response")
        block = next(
            (b for b in response.content
             if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == TOOL_NAME),
            None,
        )
        if block is None:
            raise DecomposerError("no tool call")
        payload = block.input if isinstance(block.input, dict) else {}
        atoms, warnings = parse_items(payload.get("atoms"))
        usage = {
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", None),
            "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", None),
        }
        return DecomposeResult(
            atoms=atoms, usage=[usage], warnings=warnings,
            version=version_string(self.provider, self.model),
        )
