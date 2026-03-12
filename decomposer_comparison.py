"""
decomposer_comparison.py — throwaway spike script
Compare regex vs Haiku decomposer on the same memories.
Run: uv run python decomposer_comparison.py
Requires: ANTHROPIC_API_KEY env var
"""

import asyncio
import json
import numpy as np
from anthropic import AsyncAnthropic

# Import the existing regex decomposer
from mnemo.server.decomposer import decompose

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

# Representative test memories — mix of strategic, episodic, procedural content
ORIGINAL_TEXTS = [
    "Mnemo uses Beta distributions for confidence scoring. Each atom has alpha and beta parameters that represent evidence for and against. The expected confidence is alpha/(alpha+beta). This was confirmed working in testing.",
    "I discovered that the decomposer splits sentences on periods, which breaks dotted identifiers like pd.read_csv and torch.nn.Module. The regex protects inline code blocks but not bare identifiers in prose.",
    "When deploying Mnemo, always use pgvector with the ivfflat index type. The cosine distance operator is <=> in PostgreSQL. Make sure to create the vector extension before running the schema.",
    "Agent-to-agent sharing works through views and capabilities. A view is a filtered snapshot of an agent's memory. Capabilities grant read access to specific views. The receiving agent can search the shared view semantically.",
    "I think the consolidation interval might be too aggressive at 60 minutes. Atoms that are accessed frequently should decay slower. The access_bonus in the decay function uses ln(1 + access_count) * 0.1 as a multiplier.",
    "The graph expansion uses a recursive CTE to walk edges up to N hops from seed atoms. Each hop multiplies the relevance score by edge_weight * 0.7. This means atoms 3 hops away have at most 0.343 of the original relevance.",
    "Yesterday I tested the recall endpoint with 50 stored atoms. Similarity scores capped at around 0.65 even for queries that closely matched stored content. The embedding model is all-MiniLM-L6-v2 with 384 dimensions.",
    "Best practice: when storing procedural knowledge, use imperative language like 'always do X' or 'never do Y'. The decomposer recognizes these patterns and assigns higher confidence via Beta(8,1). Hedging language like 'maybe' or 'I think' gets lower confidence.",
    "The snapshot_atoms table freezes the set of atom IDs at view creation time. This means shared views are scope-safe — graph expansion cannot pull atoms outside the snapshot. However, atoms can still decay below visibility within the snapshot.",
    "I found that duplicate detection only compares atoms of the same type. If the decomposer creates an episodic and a semantic atom with near-identical text, they won't be caught as duplicates. This wastes the result budget on recall.",
]

TEST_QUERIES = [
    "How does confidence scoring work in Mnemo?",
    "What are the issues with the decomposer?",
    "How does agent-to-agent sharing work?",
    "What is the graph expansion algorithm?",
    "How does decay affect memory retrieval?",
    "What are best practices for storing memories?",
    "How do embeddings and similarity search work?",
    "What problems were found during testing?",
]


def regex_decompose(text: str) -> list[dict]:
    """Call the existing regex decomposer."""
    atoms = decompose(text)
    return [{"text": a.text, "confidence": a.confidence_alpha / (a.confidence_alpha + a.confidence_beta)} for a in atoms]


async def haiku_decompose(client: AsyncAnthropic, text: str) -> list[dict]:
    """Call Haiku for decomposition with prompt caching."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[{
            "type": "text",
            "text": DECOMPOSER_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]  # remove opening ```json line
        raw = raw.rsplit("```", 1)[0]  # remove closing ```
    return json.loads(raw.strip())


def embed_texts(texts: list[str]) -> np.ndarray:
    """Embed a list of texts using the same model Mnemo uses."""
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2")
    return model.encode(texts, normalize_embeddings=True)


async def main():
    client = AsyncAnthropic()
    results = {}

    for label in ["regex", "haiku"]:
        all_atoms = []
        for text in ORIGINAL_TEXTS:
            if label == "regex":
                atoms = regex_decompose(text)
            else:
                atoms = await haiku_decompose(client, text)
            all_atoms.extend(atoms)

        print(f"\n{'=' * 70}")
        print(f"{label.upper()}: {len(all_atoms)} atoms from {len(ORIGINAL_TEXTS)} inputs")
        print(f"{'=' * 70}")
        for i, atom in enumerate(all_atoms):
            conf = atom.get("confidence", 0.5)
            text_preview = atom["text"][:100]
            print(f"  [{i+1:2d}] conf={conf:.2f}  {text_preview}{'...' if len(atom['text']) > 100 else ''}")

        # Embed atoms and queries
        atom_texts = [a["text"] for a in all_atoms]
        all_texts = atom_texts + TEST_QUERIES
        embeddings = embed_texts(all_texts)
        atom_embeddings = embeddings[:len(atom_texts)]
        query_embeddings = embeddings[len(atom_texts):]

        sim_matrix = query_embeddings @ atom_embeddings.T

        scores = {}
        for qi, query in enumerate(TEST_QUERIES):
            ranked = sorted(
                [(atom_texts[ai], float(sim_matrix[qi, ai])) for ai in range(len(atom_texts))],
                key=lambda x: x[1],
                reverse=True,
            )
            scores[query] = ranked

        results[label] = {"atoms": all_atoms, "scores": scores}

    # Comparison
    print(f"\n{'=' * 70}")
    print("COMPARISON: Top similarity scores per query")
    print(f"{'=' * 70}")
    deltas = []
    for query in TEST_QUERIES:
        regex_top = results["regex"]["scores"][query][0][1]
        haiku_top = results["haiku"]["scores"][query][0][1]
        delta = haiku_top - regex_top
        deltas.append(delta)
        winner = "HAIKU" if delta > 0.01 else ("REGEX" if delta < -0.01 else "TIE")
        print(f"\n  Q: {query}")
        print(f"    regex best: {regex_top:.4f}  |  haiku best: {haiku_top:.4f}  |  delta: {delta:+.4f}  [{winner}]")
        print(f"    regex top atom: {results['regex']['scores'][query][0][0][:80]}...")
        print(f"    haiku top atom: {results['haiku']['scores'][query][0][0][:80]}...")

    avg_delta = sum(deltas) / len(deltas)
    print(f"\n{'=' * 70}")
    print(f"AVERAGE DELTA: {avg_delta:+.4f}")
    if avg_delta > 0.1:
        print("VERDICT: LLM decomposer is significantly better. Ship it.")
    elif avg_delta > 0.05:
        print("VERDICT: LLM decomposer is moderately better. Likely worth shipping.")
    elif avg_delta > 0:
        print("VERDICT: LLM decomposer is marginally better. Problem may be downstream.")
    else:
        print("VERDICT: No improvement. Problem is downstream, not decomposition.")
    print(f"{'=' * 70}")


asyncio.run(main())
