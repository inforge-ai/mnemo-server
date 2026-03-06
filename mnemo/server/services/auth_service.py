import hashlib
import json
import secrets
from uuid import UUID


def generate_api_key() -> str:
    """Generate a new mnemo_ prefixed API key."""
    return "mnemo_" + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    """Return SHA-256 hex digest of the key."""
    return hashlib.sha256(key.encode()).hexdigest()


async def create_agent_with_key(
    conn,
    name: str,
    persona: str,
    domain_tags: list[str],
    key_name: str = "default",
) -> tuple[dict, str]:
    """
    Idempotent: if agent with this name exists, generates a new key for it.
    If agent does not exist, creates it and generates a key.
    Returns (agent_dict, plaintext_key).
    """
    # Look up existing agent by name
    row = await conn.fetchrow(
        "SELECT id, name, persona, domain_tags, metadata, created_at, is_active "
        "FROM agents WHERE name = $1",
        name,
    )

    if row is None:
        # Create new agent
        row = await conn.fetchrow(
            """
            INSERT INTO agents (name, persona, domain_tags, metadata)
            VALUES ($1, $2, $3, $4)
            RETURNING id, name, persona, domain_tags, metadata, created_at, is_active
            """,
            name,
            persona,
            domain_tags,
            json.dumps({}),
        )

    agent = {
        "id": str(row["id"]),
        "name": row["name"],
        "persona": row["persona"],
        "domain_tags": list(row["domain_tags"]) if row["domain_tags"] else [],
        "created_at": row["created_at"],
        "is_active": row["is_active"],
    }

    plaintext_key = await create_additional_key(conn, UUID(agent["id"]), key_name)
    return agent, plaintext_key


async def validate_api_key(conn, key: str) -> dict | None:
    """
    Look up key by SHA-256 hash. Update last_used_at if found.
    Returns agent row dict if key is valid and active, else None.
    """
    key_hash = hash_key(key)
    row = await conn.fetchrow(
        """
        SELECT k.id AS key_id, k.key_prefix, k.last_used_at,
               a.id AS agent_id, a.name, a.persona, a.domain_tags, a.is_active
        FROM api_keys k
        JOIN agents a ON a.id = k.agent_id
        WHERE k.key_hash = $1 AND k.is_active = true
        """,
        key_hash,
    )
    if row is None:
        return None

    # Update last_used_at (fire and forget — do not block on this)
    await conn.execute(
        "UPDATE api_keys SET last_used_at = now() WHERE id = $1",
        row["key_id"],
    )

    return {
        "id": str(row["agent_id"]),
        "name": row["name"],
        "persona": row["persona"],
        "domain_tags": list(row["domain_tags"]) if row["domain_tags"] else [],
        "is_active": row["is_active"],
        "key_prefix": row["key_prefix"],
        "last_used_at": row["last_used_at"],
    }


async def create_additional_key(conn, agent_id: UUID, key_name: str = "default") -> str:
    """Generate and store an additional key for an existing agent. Returns plaintext key."""
    key = generate_api_key()
    key_hash = hash_key(key)
    key_prefix = key[:16]

    await conn.execute(
        """
        INSERT INTO api_keys (agent_id, key_hash, key_prefix, name, is_active)
        VALUES ($1, $2, $3, $4, true)
        """,
        agent_id,
        key_hash,
        key_prefix,
        key_name,
    )
    return key
