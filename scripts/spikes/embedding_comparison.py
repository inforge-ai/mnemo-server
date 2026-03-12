"""
embedding_comparison.py — Haiku atoms scored against multiple embedding models.
Run: uv run python embedding_comparison.py [--embedding-model MODEL_NAME]
Without --embedding-model, runs all four models and prints a summary table.
Requires: ANTHROPIC_API_KEY env var (or .env file)
"""

import argparse
import asyncio
import json
import numpy as np
from anthropic import AsyncAnthropic

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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

ALL_MODELS = [
    "sentence-transformers/all-MiniLM-L6-v2",
    "BAAI/bge-small-en-v1.5",
    "thenlper/gte-small",
    "BAAI/bge-base-en-v1.5",
]


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
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]
    return json.loads(raw.strip())


def embed_texts(texts: list[str], model_name: str) -> np.ndarray:
    """Embed texts with the specified sentence-transformers model."""
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(model_name)
    return model.encode(texts, normalize_embeddings=True)


def score_model(atom_texts: list[str], model_name: str) -> dict:
    """Embed Haiku atoms + queries with one model, return per-query top-1 scores."""
    print(f"\n  Embedding with {model_name}...", end=" ", flush=True)
    all_texts = atom_texts + TEST_QUERIES
    embeddings = embed_texts(all_texts, model_name)
    atom_embeddings = embeddings[:len(atom_texts)]
    query_embeddings = embeddings[len(atom_texts):]
    dims = embeddings.shape[1]

    sim_matrix = query_embeddings @ atom_embeddings.T

    per_query = {}
    for qi, query in enumerate(TEST_QUERIES):
        ranked = sorted(
            [(atom_texts[ai], float(sim_matrix[qi, ai])) for ai in range(len(atom_texts))],
            key=lambda x: x[1],
            reverse=True,
        )
        per_query[query] = ranked

    avg_top1 = np.mean([per_query[q][0][1] for q in TEST_QUERIES])
    print(f"dims={dims}, avg top-1={avg_top1:.4f}")
    return {"per_query": per_query, "avg_top1": float(avg_top1), "dims": dims}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--embedding-model",
        type=str,
        default=None,
        help="Run a single embedding model (e.g. BAAI/bge-small-en-v1.5). Omit to run all four.",
    )
    args = parser.parse_args()

    models_to_test = [args.embedding_model] if args.embedding_model else ALL_MODELS

    # Step 1: Decompose with Haiku (once, reuse across all models)
    print("=" * 70)
    print("Step 1: Decomposing with Haiku...")
    print("=" * 70)
    client = AsyncAnthropic()
    all_atoms = []
    for text in ORIGINAL_TEXTS:
        atoms = await haiku_decompose(client, text)
        all_atoms.extend(atoms)

    atom_texts = [a["text"] for a in all_atoms]
    print(f"  {len(all_atoms)} atoms from {len(ORIGINAL_TEXTS)} inputs")
    for i, atom in enumerate(all_atoms):
        conf = atom.get("confidence", 0.5)
        preview = atom["text"][:90]
        print(f"  [{i+1:2d}] conf={conf:.2f}  {preview}{'...' if len(atom['text']) > 90 else ''}")

    # Step 2: Score each embedding model
    print(f"\n{'=' * 70}")
    print("Step 2: Scoring embedding models on Haiku atoms")
    print("=" * 70)

    results = {}
    for model_name in models_to_test:
        results[model_name] = score_model(atom_texts, model_name)

    # Step 3: Per-query breakdown
    print(f"\n{'=' * 70}")
    print("PER-QUERY BREAKDOWN")
    print("=" * 70)
    for query in TEST_QUERIES:
        print(f"\n  Q: {query}")
        best_score = -1
        best_model = ""
        for model_name in models_to_test:
            top_atom, top_score = results[model_name]["per_query"][query][0]
            marker = ""
            if top_score > best_score:
                best_score = top_score
                best_model = model_name
            print(f"    {model_name:45s}  top-1={top_score:.4f}  [{top_atom[:60]}...]")
        # Mark the winner
        print(f"    --> BEST: {best_model}")

    # Step 4: Summary table
    print(f"\n{'=' * 70}")
    print("SUMMARY TABLE")
    print("=" * 70)
    print(f"  {'Model':<45s}  {'Dims':>4s}  {'Avg Top-1':>9s}  {'#Wins':>5s}")
    print(f"  {'-'*45}  {'-'*4}  {'-'*9}  {'-'*5}")

    # Count wins per model
    win_counts = {m: 0 for m in models_to_test}
    for query in TEST_QUERIES:
        best_score = -1
        best_model = ""
        for model_name in models_to_test:
            top_score = results[model_name]["per_query"][query][0][1]
            if top_score > best_score:
                best_score = top_score
                best_model = model_name
        win_counts[best_model] += 1

    # Sort by avg_top1 descending
    ranked_models = sorted(models_to_test, key=lambda m: results[m]["avg_top1"], reverse=True)
    for model_name in ranked_models:
        r = results[model_name]
        marker = " <-- CURRENT" if model_name == "sentence-transformers/all-MiniLM-L6-v2" else ""
        print(f"  {model_name:<45s}  {r['dims']:>4d}  {r['avg_top1']:>9.4f}  {win_counts[model_name]:>5d}{marker}")

    winner = ranked_models[0]
    print(f"\n  WINNER: {winner} (dims={results[winner]['dims']})")
    if results[winner]["dims"] != 384:
        print(f"  NOTE: {winner} uses {results[winner]['dims']} dims vs current 384.")
        print(f"  This requires: ALTER the embedding column type, re-embed ~30 existing atoms.")
        print(f"  With ~30 atoms in the DB, this is trivial.")
    print("=" * 70)


asyncio.run(main())
