"""
LLM-based decomposer using Anthropic Haiku with prompt caching.

Replaces the regex decomposer for higher-quality atom extraction.
Confidence is inferred by the LLM and mapped to Beta distribution parameters.

Prompt caching: The system prompt is marked with cache_control=ephemeral so
identical system prompts within a 5-minute window are served from cache.
"""

import json
import logging
from dataclasses import dataclass
from functools import lru_cache

from anthropic import AsyncAnthropic

from .decomposer import DecomposedAtom


@dataclass
class DecomposerResult:
    """Bundle of decomposed atoms + optional LLM usage metadata."""
    atoms: list[DecomposedAtom]
    usage: dict | None = None  # {model, input_tokens, output_tokens, cache_*}

logger = logging.getLogger(__name__)

DECOMPOSER_PROMPT = """You are a memory decomposer. Given a block of text, extract discrete knowledge atoms.

Rules:
- Each atom should be ONE coherent claim, fact, or observation
- Preserve specificity — don't over-generalise
- Don't split tightly coupled facts into separate atoms
- Return JSON array of objects: {"text": "...", "type": "episodic|semantic|procedural", "confidence": 0.0-1.0}
- Confidence should reflect how certain/well-supported the claim is in the source text

Types:
- episodic: A specific experience, event, or observation tied to a moment in time.
  "I discovered that row 847 had a string in the account_id column."
- semantic: A general fact about how something works, independent of any specific event.
  "pandas.read_csv silently coerces mixed-type columns."
- procedural: A rule, practice, or instruction for future behavior.
  "Always specify dtype explicitly when using read_csv."

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


async def llm_decompose(text: str) -> DecomposerResult:
    """Decompose text into atoms using Haiku with prompt caching.

    Returns DecomposerResult containing atoms + token usage metadata.
    The LLM classifies each atom as episodic, semantic, or procedural.
    """
    if not text or not text.strip():
        return DecomposerResult(atoms=[])

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

    # Extract usage metadata for cost tracking
    usage = {
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", None),
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", None),
    }

    raw_text = response.content[0].text
    # Strip markdown code fences if the model wraps JSON in ```
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]
        raw_text = raw_text.rsplit("```", 1)[0]
    raw = json.loads(raw_text.strip())
    atoms = []
    for item in raw:
        alpha, beta = _confidence_to_beta(item.get("confidence", 0.5))
        atom_type = item.get("type", "semantic")
        if atom_type not in ("episodic", "semantic", "procedural"):
            atom_type = "semantic"
        atoms.append(DecomposedAtom(
            text=item["text"],
            atom_type=atom_type,
            confidence_alpha=alpha,
            confidence_beta=beta,
            source_type="direct_experience",
        ))

    return DecomposerResult(atoms=atoms, usage=usage)
