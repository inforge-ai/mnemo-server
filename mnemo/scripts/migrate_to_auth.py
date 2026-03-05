"""
Migration script: generate API keys for all existing active agents.

Usage:
    uv run python -m mnemo.scripts.migrate_to_auth [--base-url http://localhost:8000]

For each active agent in the database:
  1. Call POST /v1/auth/register (idempotent — creates a new key, reuses agent)
  2. Print the API key with agent name and ID
  3. Exit with instructions to update service files
"""

import asyncio
import sys

import httpx
import asyncpg
import os


BASE_URL = os.environ.get("MNEMO_BASE_URL", "http://localhost:8000")
DB_URL = os.environ.get("MNEMO_DATABASE_URL", "postgresql://mnemo:mnemo@localhost:5432/mnemo")


async def main():
    # Fetch all active agents from DB directly (REST /v1/agents requires exact name)
    conn = await asyncpg.connect(DB_URL)
    agents = await conn.fetch(
        "SELECT id, name, persona, domain_tags FROM agents WHERE is_active = true ORDER BY created_at ASC"
    )
    await conn.close()

    if not agents:
        print("No active agents found.")
        return

    print(f"Generating API keys for {len(agents)} existing agent(s)...\n")

    results = []
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        for agent in agents:
            resp = await client.post(
                "/v1/auth/register",
                json={
                    "name": agent["name"],
                    "persona": agent["persona"] or "",
                    "domain_tags": list(agent["domain_tags"]) if agent["domain_tags"] else [],
                    "key_name": "migrated",
                },
            )
            if resp.status_code not in (200, 201):
                print(f"  ERROR for {agent['name']}: {resp.status_code} {resp.text}", file=sys.stderr)
                continue
            data = resp.json()
            results.append((agent["name"], data["agent_id"], data["api_key"]))

    if not results:
        print("No keys generated.", file=sys.stderr)
        sys.exit(1)

    max_name = max(len(r[0]) for r in results)
    for name, agent_id, key in results:
        print(f"{name:<{max_name}}  ({agent_id}) : {key}")

    print(
        "\nDone. Update your service files with MNEMO_API_KEY=<key>.\n"
        "Enable auth when ready: MNEMO_AUTH_ENABLED=true"
    )


if __name__ == "__main__":
    asyncio.run(main())
