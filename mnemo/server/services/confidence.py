"""Initial confidence (docs/phase2-design.md §8).

prompt_v3 gives no confidence: the Beta prior comes from the source's
origin_class. The Jev grounding answer scales it (a flag, not a gate), and
the deterministic ungrounded_quote check still lowers one band.
"""

from __future__ import annotations

from ..decomposers.base import BETA_LADDER, lower_one_band

ORIGIN_PRIORS: dict[str, tuple[float, float]] = {
    "measurement": (8.0, 1.0),
    "document": (6.0, 1.5),
    "professional": (6.0, 2.0),
    "user_stated": (5.0, 2.0),
    "unspecified": (4.0, 2.0),
    "agent_inference": (3.0, 2.0),
}
UNGROUNDED_SCALE = 0.5


def prior_for(origin_class: str | None) -> tuple[float, float]:
    return ORIGIN_PRIORS.get(origin_class or "unspecified", ORIGIN_PRIORS["unspecified"])


def scale_alpha(alpha: float, beta: float, s: float) -> tuple[float, float]:
    """α' = 1 + (α − 1)·s."""
    return 1.0 + (alpha - 1.0) * s, beta


def lower_band(alpha: float, beta: float) -> tuple[float, float]:
    """One band down: the ladder step for ladder values, else halve α − 1."""
    if (alpha, beta) in BETA_LADDER:
        return lower_one_band(alpha, beta)
    return scale_alpha(alpha, beta, 0.5)
