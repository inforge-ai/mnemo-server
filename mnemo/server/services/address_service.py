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


IDENTIFIER_FORMS = (
    "use the agent's UUID, its name (e.g. clio) or its address (e.g. clio:tom.inforge)"
)


async def _fetchval(pool_or_conn, query: str, *args):
    if isinstance(pool_or_conn, asyncpg.Pool):
        async with pool_or_conn.acquire() as conn:
            return await conn.fetchval(query, *args)
    return await pool_or_conn.fetchval(query, *args)


async def resolve_agent_identifier(pool_or_conn, identifier: str, operator_id: UUID | None = None) -> UUID:
    """UUID, address (name:user.org) or — v2 Phase 1 — a bare agent name,
    resolved within the caller's operator. Names are unique per operator
    (agents_operator_name_unique), so a name resolves to at most one agent."""
    try:
        return UUID(identifier)
    except ValueError:
        pass
    if ":" in identifier:
        agent_id = await resolve_address(pool_or_conn, identifier)
    elif operator_id is not None:
        agent_id = await _fetchval(
            pool_or_conn,
            "SELECT id FROM agents WHERE operator_id = $1 AND name = $2",
            operator_id, identifier,
        )
    else:
        agent_id = None
    if not agent_id:
        raise HTTPException(404, f"Agent not found: {identifier!r}; {IDENTIFIER_FORMS}")
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
            WHERE a.status = 'active'
        """)
        for agent in agents:
            address = build_address(agent["name"], agent["username"], agent["org"])
            await conn.execute("""
                INSERT INTO agent_addresses (agent_id, address)
                VALUES ($1, $2)
                ON CONFLICT (agent_id) DO UPDATE SET address = $2
            """, agent["id"], address)
    return len(agents)
