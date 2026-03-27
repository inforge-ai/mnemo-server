#!/usr/bin/env python3
"""
RBAC-Lite migration script.

Generates new prefixed keys for all existing operators and agents:
  - Operators: mnemo_op_ prefix, stored in api_keys table
  - Agents: mnemo_ag_ prefix, stored in agents.key_hash/key_prefix columns

Outputs a credential table to stdout. Deactivates old api_keys.

Usage:
    MNEMO_DATABASE_URL=postgresql://... uv run python scripts/migrate_rbac.py
"""

import asyncio
import os
import sys

import asyncpg

# Add project root to path so we can import mnemo modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo.server.services.auth_service import (
    generate_agent_key,
    generate_operator_key,
    hash_key,
)


async def main():
    database_url = os.environ.get("MNEMO_DATABASE_URL")
    if not database_url:
        print("ERROR: Set MNEMO_DATABASE_URL environment variable")
        sys.exit(1)

    conn = await asyncpg.connect(database_url)

    try:
        print("=" * 80)
        print("RBAC-Lite Key Migration")
        print("=" * 80)

        # --- Operators ---
        operators = await conn.fetch(
            "SELECT id, name, username, org, status FROM operators ORDER BY created_at"
        )
        print(f"\nFound {len(operators)} operators")

        operator_credentials = []
        for op in operators:
            # Generate new operator key with mnemo_op_ prefix
            new_key = generate_operator_key()
            new_hash = hash_key(new_key)
            new_prefix = new_key[:16]

            # Deactivate all old keys for this operator
            await conn.execute(
                "UPDATE api_keys SET is_active = false WHERE operator_id = $1",
                op["id"],
            )

            # Insert new key
            await conn.execute(
                """
                INSERT INTO api_keys (operator_id, key_hash, key_prefix, name, is_active)
                VALUES ($1, $2, $3, 'rbac-migration', true)
                """,
                op["id"],
                new_hash,
                new_prefix,
            )

            operator_credentials.append({
                "type": "operator",
                "name": op["name"],
                "username": op["username"],
                "org": op["org"],
                "status": op["status"],
                "new_key": new_key,
            })

        # --- Agents ---
        agents = await conn.fetch(
            """
            SELECT a.id, a.name, a.status, aa.address, o.username AS operator_username
            FROM agents a
            LEFT JOIN agent_addresses aa ON aa.agent_id = a.id
            JOIN operators o ON o.id = a.operator_id
            WHERE a.status = 'active'
            ORDER BY a.created_at
            """
        )
        print(f"Found {len(agents)} active agents")

        agent_credentials = []
        for ag in agents:
            new_key = generate_agent_key()
            new_hash = hash_key(new_key)
            new_prefix = new_key[:16]

            await conn.execute(
                "UPDATE agents SET key_hash = $1, key_prefix = $2 WHERE id = $3",
                new_hash,
                new_prefix,
                ag["id"],
            )

            agent_credentials.append({
                "type": "agent",
                "name": ag["name"],
                "address": ag["address"],
                "operator": ag["operator_username"],
                "new_key": new_key,
            })

        # --- Output credential table ---
        print("\n" + "=" * 80)
        print("NEW CREDENTIALS (save these — they will not be shown again)")
        print("=" * 80)

        print("\n--- Operator Keys ---")
        print(f"{'Username':<20} {'Org':<15} {'Status':<10} {'New Key'}")
        print("-" * 100)
        for cred in operator_credentials:
            print(f"{cred['username']:<20} {cred['org']:<15} {cred['status']:<10} {cred['new_key']}")

        print(f"\n--- Agent Keys ---")
        print(f"{'Address':<45} {'Operator':<15} {'New Key'}")
        print("-" * 120)
        for cred in agent_credentials:
            addr = cred["address"] or cred["name"]
            print(f"{addr:<45} {cred['operator']:<15} {cred['new_key']}")

        print(f"\n{'=' * 80}")
        print(f"Migration complete: {len(operator_credentials)} operators, {len(agent_credentials)} agents")
        print(f"Old operator keys have been deactivated.")
        print(f"{'=' * 80}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
