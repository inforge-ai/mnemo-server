"""
reclassify_atoms.py — Reclassify atom types for LLM-decomposed atoms.

The LLM decomposer originally hardcoded all atoms as "semantic". This script
sends each affected atom's text to Haiku for type classification and updates
atom_type, decay_half_life_days, and decomposer_version.

Usage:
    uv run scripts/reclassify_atoms.py              # dry run (shows counts)
    uv run scripts/reclassify_atoms.py --apply      # actually update atoms

Requires ANTHROPIC_API_KEY in environment.
Safe to run multiple times — only targets decomposer_version = 'haiku_v1'.
"""

import argparse
import asyncio
import json
import sys

import asyncpg
from anthropic import AsyncAnthropic

from mnemo.server.config import settings

HALF_LIVES = {
    "episodic": settings.decay_episodic,
    "semantic": settings.decay_semantic,
    "procedural": settings.decay_procedural,
    "relational": settings.decay_relational,
}

CLASSIFY_PROMPT = """Classify each memory atom as one of: episodic, semantic, procedural.

Types:
- episodic: A specific experience, event, or observation tied to a moment in time.
- semantic: A general fact about how something works, independent of any specific event.
- procedural: A rule, practice, or instruction for future behavior.

Return a JSON array of objects: {"id": <id>, "type": "episodic|semantic|procedural"}
Return ONLY the JSON array, no other text."""

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 50


async def classify_batch(
    client: AsyncAnthropic, batch: list[dict],
) -> dict[int, str]:
    """Send a batch of atoms to Haiku for classification. Returns {index: type}."""
    numbered = "\n".join(
        f'{{"id": {i}, "text": {json.dumps(item["text_content"])}}}'
        for i, item in enumerate(batch)
    )

    response = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": CLASSIFY_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": numbered}],
    )

    raw_text = response.content[0].text
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]
        raw_text = raw_text.rsplit("```", 1)[0]

    results = json.loads(raw_text.strip())
    classifications = {}
    for item in results:
        atom_type = item.get("type", "semantic")
        if atom_type not in ("episodic", "semantic", "procedural"):
            atom_type = "semantic"
        classifications[item["id"]] = atom_type

    return classifications


async def main(apply: bool) -> None:
    print(f"Database: {settings.database_url}")

    conn = await asyncpg.connect(settings.database_url)

    rows = await conn.fetch(
        "SELECT id, text_content, atom_type FROM atoms "
        "WHERE decomposer_version = 'haiku_v1' AND is_active = true "
        "ORDER BY created_at"
    )
    print(f"Atoms to reclassify: {len(rows)}")

    if len(rows) == 0:
        print("Nothing to do.")
        await conn.close()
        return

    if not apply:
        print("Dry run — pass --apply to update atoms.")
        await conn.close()
        return

    client = AsyncAnthropic()
    updated = 0
    reclassified = {"episodic": 0, "semantic": 0, "procedural": 0}

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        classifications = await classify_batch(client, batch)

        for j, row in enumerate(batch):
            atom_type = classifications.get(j, "semantic")
            half_life = HALF_LIVES[atom_type]

            await conn.execute(
                "UPDATE atoms SET atom_type = $1, decay_half_life_days = $2, "
                "decomposer_version = 'haiku_v1_retyped' WHERE id = $3",
                atom_type, half_life, row["id"],
            )
            reclassified[atom_type] += 1
            updated += 1

        print(f"  {updated}/{len(rows)}...")

    print(f"\nDone — {updated} atoms reclassified:")
    for t, count in reclassified.items():
        print(f"  {t}: {count}")

    remaining = await conn.fetchval(
        "SELECT COUNT(*) FROM atoms WHERE decomposer_version = 'haiku_v1' AND is_active = true"
    )
    print(f"\nRemaining haiku_v1 atoms: {remaining}")
    await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reclassify LLM-decomposed atom types.")
    parser.add_argument("--apply", action="store_true", help="Actually update atoms")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
