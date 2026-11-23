"""Tier 2 date repair, step 2 of 3: embed the planned new texts.

Runs INSIDE the server container (the real embedder and the app's DB role):

    docker exec -i mnemo-server-mnemo-server-1 sh -c 'cd /app && .venv/bin/python -' \
        < scripts/phase1/remembered_on_tier2_embed.py

Reads repair_date_text_2026_11 rows with action='replace' and no new_embedding,
embeds new_text exactly as the store path does (embeddings.encode_batch,
"document" prompt), and writes new_embedding. Atoms are not touched; step 3
(remembered_on_tier2_apply.sql) swaps text, date and embedding in one
transaction. Prints counts only.
"""

import asyncio
import json

import asyncpg
from pgvector.asyncpg import register_vector

from mnemo.server.config import settings
from mnemo.server.embeddings import encode_batch

BATCH = 32


async def main() -> None:
    conn = await asyncpg.connect(settings.database_url)
    await register_vector(conn)
    try:
        rows = await conn.fetch(
            "SELECT atom_id, new_text FROM repair_date_text_2026_11 "
            "WHERE action = 'replace' AND new_embedding IS NULL AND reverted_at IS NULL AND applied_at IS NULL "
            "ORDER BY atom_id"
        )
        done = 0
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            vectors = await encode_batch([r["new_text"] for r in chunk])
            async with conn.transaction():
                for r, v in zip(chunk, vectors):
                    await conn.execute(
                        "UPDATE repair_date_text_2026_11 SET new_embedding = $2::vector "
                        "WHERE atom_id = $1 AND new_embedding IS NULL",
                        r["atom_id"], v,
                    )
            done += len(chunk)
        left = await conn.fetchval(
            "SELECT count(*) FROM repair_date_text_2026_11 "
            "WHERE action = 'replace' AND new_embedding IS NULL AND reverted_at IS NULL AND applied_at IS NULL"
        )
        dims = await conn.fetchval(
            "SELECT min(vector_dims(new_embedding)) || '-' || max(vector_dims(new_embedding)) "
            "FROM repair_date_text_2026_11 WHERE action = 'replace' AND new_embedding IS NOT NULL"
        )
        # Same encoding as the store path? Re-embed a sample of the OLD texts and
        # compare with the stored old embeddings (expect cosine ~1.0).
        sample = await conn.fetch(
            "SELECT old_text, old_embedding FROM repair_date_text_2026_11 "
            "WHERE action = 'replace' AND old_text IS NOT NULL ORDER BY atom_id LIMIT 8"
        )
        check = None
        if sample:
            again = await encode_batch([r["old_text"] for r in sample])
            cos = []
            for r, v in zip(sample, again):
                a = list(r["old_embedding"])
                dot = sum(x * y for x, y in zip(a, v))
                na = sum(x * x for x in a) ** 0.5
                nb = sum(y * y for y in v) ** 0.5
                cos.append(float(dot / (na * nb)))
            check = {"n": len(cos), "min_cosine": round(min(cos), 6)}
        print(json.dumps({"to_embed": len(rows), "embedded": done, "still_missing": left, "dims": dims,
                          "old_text_reembed_check": check}))
    finally:
        await conn.close()


asyncio.run(main())
