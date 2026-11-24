"""Decision-model protocol (docs/phase2-design.md §5).

A decision model answers fixed questions about a `state` text:
  choice  -> {"type": "choice", "choice": key, "probabilities": {key: p}}
  noul    -> {"type": "noul", "noul": p}
  refusal -> {"type": "refusal"}
`ask` never raises: on failure it returns a DecisionResult with answers None
and `error` set, so the store path can fail open (relation `pending`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


def choice(instructions: str, criteria: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def noul(instructions: str, true: str, false: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": true, "false": false}}


@dataclass
class DecisionResult:
    answers: dict[str, dict] | None          # question key -> answer; None on failure
    model: str | None = None                 # model that answered (None if none did)
    latency_ms: int = 0                      # wall time across all attempts
    cost: float | None = None                # usage.cost of the successful request
    attempts: int = 0                        # HTTP requests sent, all models
    error: str | None = None                 # plain reason when answers is None
    errors: list[str] = field(default_factory=list)   # one per failed attempt

    @property
    def ok(self) -> bool:
        return self.answers is not None

    def probabilities(self, key: str) -> dict[str, float] | None:
        a = (self.answers or {}).get(key) or {}
        return a.get("probabilities") if a.get("type") == "choice" else None

    def p_true(self, key: str) -> float | None:
        a = (self.answers or {}).get(key) or {}
        return a.get("noul") if a.get("type") == "noul" else None


class DecisionModel(Protocol):
    async def ask(self, state: str, questions: dict[str, dict], *, deadline: float | None = None) -> DecisionResult:
        """`deadline`: absolute time.monotonic() after which no new attempt starts."""
        ...
