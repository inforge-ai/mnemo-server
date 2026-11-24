"""Scripted decision model for tests (docs/phase2-design.md §10)."""

from __future__ import annotations

from typing import Callable

from .base import DecisionResult

Script = Callable[[str, dict[str, dict]], dict[str, dict]]


class FakeDecisions:
    """`script(state, questions) -> answers`. If it raises, `ask` returns a
    failed result (as the real client does), so outage paths can be tested."""

    def __init__(self, script: Script | None = None, model: str = "fake/decisions"):
        self.script = script or default_script
        self.model = model
        self.calls: list[tuple[str, dict[str, dict]]] = []

    async def ask(self, state, questions, *, deadline=None) -> DecisionResult:
        self.calls.append((state, questions))
        try:
            answers = self.script(state, questions)
        except Exception as exc:
            return DecisionResult(answers=None, attempts=1, error=f"fake: {exc}")
        return DecisionResult(answers=answers, model=self.model, attempts=1, cost=0.0)


def default_script(state: str, questions: dict[str, dict]) -> dict[str, dict]:
    """Uniform choices, p=0.5 yes/no: decides nothing confidently."""
    out = {}
    for key, q in questions.items():
        if q["type"] == "choice":
            keys = list(q["criteria"])
            out[key] = {"type": "choice", "choice": keys[0], "probabilities": {k: 1 / len(keys) for k in keys}}
        else:
            out[key] = {"type": "noul", "noul": 0.5}
    return out
