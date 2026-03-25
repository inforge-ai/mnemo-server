"""
re_embed.py — Re-embed all active atoms using the current embedding model.

Required after swapping embedding models (e.g. MiniLM → gte-small) so that
stored embeddings are compatible with query embeddings at recall time.

Usage:
    uv run scripts/re_embed.py              # dry run (shows count)
    uv run scripts/re_embed.py --apply      # actually update embeddings

Safe to run multiple times — idempotent.
"""

import argparse
import asyncio
import sys

import asyncpg
from pgvector.asyncpg import register_vector
from sentence_transformers import SentenceTransformer

from mnemo.server.config import settings


async def main(apply: bool) -> None:
    model = SentenceTransformer(settings.embedding_model)
    print(f"Model: {settings.embedding_model}")
    print(f"Database: {settings.database_url}")

    conn = await asyncpg.connect(settings.database_url)
    await register_vector(conn)

    rows = await conn.fetch(
        "SELECT id, text_content FROM atoms WHERE is_active = true ORDER BY created_at"
    )
    print(f"Active atoms to re-embed: {len(rows)}")

    if not apply:
        print("Dry run — pass --apply to update embeddings.")
        await conn.close()
        return

    updated = 0
    for row in rows:
        kwargs = {"normalize_embeddings": True}
        if model.prompts:
            kwargs["prompt_name"] = "document"
        embedding = model.encode(row["text_content"], **kwargs).tolist()
        await conn.execute(
            "UPDATE atoms SET embedding = $1::vector WHERE id = $2",
            embedding,
            row["id"],
        )
        updated += 1
        if updated % 10 == 0:
            print(f"  {updated}/{len(rows)}...")

    print(f"Done — {updated} atoms re-embedded with {settings.embedding_model}.")
    await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-embed all active atoms.")
    parser.add_argument("--apply", action="store_true", help="Actually update embeddings")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
