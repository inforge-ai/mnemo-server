"""
LLM-based decomposer using Anthropic Haiku with prompt caching.

Replaces the regex decomposer for higher-quality atom extraction.
Confidence is inferred by the LLM and mapped to Beta distribution parameters.

Prompt caching: The system prompt is marked with cache_control=ephemeral so
identical system prompts within a 5-minute window are served from cache.
"""

import json
import logging
from functools import lru_cache

from anthropic import AsyncAnthropic

from .decomposer import DecomposedAtom

logger = logging.getLogger(__name__)

DECOMPOSER_PROMPT = """You are a memory decomposer. Given a block of text, extract discrete knowledge atoms.

Rules:
- Each atom should be ONE coherent claim, fact, or observation
- Preserve specificity — don't over-generalise
- Don't split tightly coupled facts into separate atoms
- If the text describes an event, capture the event as one atom
- If the text states a general fact, capture it as one atom
- Return JSON array of objects: {"text": "...", "confidence": 0.0-1.0}
- Confidence should reflect how certain/well-supported the claim is in the source text

Return ONLY the JSON array, no other text."""

MODEL = "claude-haiku-4-5-20251001"


@lru_cache(maxsize=1)
def _get_client() -> AsyncAnthropic:
    """Singleton Anthropic client. Reads ANTHROPIC_API_KEY from env."""
    return AsyncAnthropic()


def _confidence_to_beta(confidence: float) -> tuple[float, float]:
    """Map LLM-assigned confidence [0,1] to Beta distribution parameters.

    Bands match the regex decomposer's output so decay behaviour is consistent:
      >= 0.8  -> Beta(8, 1)   high confidence
      >= 0.6  -> Beta(4, 2)   moderate
      >= 0.4  -> Beta(3, 2)   mild
      >= 0.25 -> Beta(2, 3)   low
      <  0.25 -> Beta(2, 4)   very low
    """
    if confidence >= 0.8:
        return (8.0, 1.0)
    elif confidence >= 0.6:
        return (4.0, 2.0)
    elif confidence >= 0.4:
        return (3.0, 2.0)
    elif confidence >= 0.25:
        return (2.0, 3.0)
    else:
        return (2.0, 4.0)


async def llm_decompose(text: str) -> list[DecomposedAtom]:
    """Decompose text into atoms using Haiku with prompt caching.

    Returns DecomposedAtom list compatible with the existing store pipeline.
    All atoms are typed 'semantic' — the LLM focuses on content quality,
    not type classification (which Task 4 removes from retrieval anyway).
    """
    if not text or not text.strip():
        return []

    client = _get_client()
    response = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": DECOMPOSER_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": text}],
    )

    raw_text = response.content[0].text
    # Strip markdown code fences if the model wraps JSON in ```
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]
        raw_text = raw_text.rsplit("```", 1)[0]
    raw = json.loads(raw_text.strip())
    atoms = []
    for item in raw:
        alpha, beta = _confidence_to_beta(item.get("confidence", 0.5))
        atoms.append(DecomposedAtom(
            text=item["text"],
            atom_type="semantic",
            confidence_alpha=alpha,
            confidence_beta=beta,
            source_type="direct_experience",
        ))

    return atoms
