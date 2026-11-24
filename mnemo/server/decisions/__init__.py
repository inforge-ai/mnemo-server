"""Decision models for v2 Phase 2 relations (docs/phase2-design.md §2, §5)."""

from .base import DecisionModel, DecisionResult, choice, noul

__all__ = ["DecisionModel", "DecisionResult", "choice", "noul", "get_decision_model"]


def get_decision_model() -> DecisionModel | None:
    from ..config import settings
    if settings.decision_provider == "openrouter":
        from .openrouter import OpenRouterDecisions
        return OpenRouterDecisions()
    if settings.decision_provider == "fake":
        from .fake import FakeDecisions
        return FakeDecisions()
    return None
