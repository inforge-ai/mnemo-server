"""Agent address validation, resolution, and management."""

import re
from uuid import UUID

import asyncpg
from fastapi import HTTPException

ADDRESS_PATTERN = re.compile(
    r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?:[a-z0-9]([a-z0-9-]*[a-z0-9])?\.[a-z0-9]([a-z0-9-]*[a-z0-9])?$'
)
MAX_ADDRESS_LENGTH = 200


def validate_address(address: str) -> bool:
    address = address.lower()
    if len(address) > MAX_ADDRESS_LENGTH:
        return False
    return bool(ADDRESS_PATTERN.match(address))


def build_address(agent_name: str, operator_username: str, operator_org: str) -> str:
    return f"{agent_name}:{operator_username}.{operator_org}".lower()


async def resolve_address(pool_or_conn, address: str) -> UUID | None:
    if isinstance(pool_or_conn, asyncpg.Pool):
        async with pool_or_conn.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT agent_id FROM agent_addresses WHERE address = $1", address.lower())
    else:
        row = await pool_or_conn.fetchrow(
            "SELECT agent_id FROM agent_addresses WHERE address = $1", address.lower())
    return row["agent_id"] if row else None


async def resolve_agent_identifier(pool_or_conn, identifier: str) -> UUID:
    try:
        return UUID(identifier)
    except ValueError:
        agent_id = await resolve_address(pool_or_conn, identifier)
        if not agent_id:
            raise HTTPException(404, f"Agent not found: {identifier}")
        return agent_id


async def create_address(conn: asyncpg.Connection, agent_id: UUID, agent_name: str,
                         operator_username: str, operator_org: str) -> str:
    address = build_address(agent_name, operator_username, operator_org)
    await conn.execute("""
        INSERT INTO agent_addresses (agent_id, address)
        VALUES ($1, $2)
        ON CONFLICT (agent_id) DO UPDATE SET address = $2
    """, agent_id, address)
    return address


async def backfill_addresses(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        agents = await conn.fetch("""
            SELECT a.id, a.name, o.username, o.org
            FROM agents a JOIN operators o ON o.id = a.operator_id
            WHERE a.is_active = true
        """)
        for agent in agents:
            address = build_address(agent["name"], agent["username"], agent["org"])
            await conn.execute("""
                INSERT INTO agent_addresses (agent_id, address)
                VALUES ($1, $2)
                ON CONFLICT (agent_id) DO UPDATE SET address = $2
            """, agent["id"], address)
    return len(agents)
