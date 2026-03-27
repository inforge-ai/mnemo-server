import hashlib
import re
import secrets
from uuid import UUID


def generate_api_key() -> str:
    """Generate a new mnemo_ prefixed API key (legacy, use generate_operator_key)."""
    return "mnemo_" + secrets.token_urlsafe(32)


def generate_operator_key() -> str:
    """Generate a new operator key with mnemo_op_ prefix."""
    return "mnemo_op_" + secrets.token_urlsafe(32)


def generate_agent_key() -> str:
    """Generate a new agent key with mnemo_ag_ prefix."""
    return "mnemo_ag_" + secrets.token_urlsafe(32)


def generate_admin_key() -> str:
    """Generate a new admin key with mnemo_admin_ prefix."""
    return "mnemo_admin_" + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    """Return SHA-256 hex digest of the key."""
    return hashlib.sha256(key.encode()).hexdigest()


async def create_operator_with_key(
    conn,
    name: str,
    email: str | None = None,
    username: str | None = None,
    org: str = "mnemo",
    key_name: str = "default",
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
) -> tuple[dict, str]:
    """
    Create a new operator and generate an API key.
    Returns (operator_dict, plaintext_key).
    Raises asyncpg.UniqueViolationError if name already exists.
    """
    if username is None:
        username = re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')

    row = await conn.fetchrow(
        """
        INSERT INTO operators (name, email, username, org, stripe_customer_id, stripe_subscription_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, name, email, username, org, created_at, status
        """,
        name,
        email,
        username,
        org,
        stripe_customer_id,
        stripe_subscription_id,
    )

    operator = {
        "id": str(row["id"]),
        "name": row["name"],
        "email": row["email"],
        "username": row["username"],
        "org": row["org"],
        "created_at": row["created_at"],
        "status": row["status"],
    }

    plaintext_key = await create_operator_key(conn, UUID(operator["id"]), key_name)
    return operator, plaintext_key


async def validate_api_key(conn, key: str) -> dict | None:
    """
    Look up key by SHA-256 hash. Update last_used_at if found.
    Returns operator dict if key is valid and active, else None.
    """
    key_hash = hash_key(key)
    row = await conn.fetchrow(
        """
        SELECT k.id AS key_id, k.key_prefix, k.last_used_at,
               o.id AS operator_id, o.name, o.email, o.status AS operator_status
        FROM api_keys k
        JOIN operators o ON o.id = k.operator_id
        WHERE k.key_hash = $1 AND k.is_active = true
        """,
        key_hash,
    )
    if row is None:
        return None

    if row["operator_status"] != "active":
        return None

    # Update last_used_at
    await conn.execute(
        "UPDATE api_keys SET last_used_at = now() WHERE id = $1",
        row["key_id"],
    )

    return {
        "id": str(row["operator_id"]),
        "name": row["name"],
        "email": row["email"],
        "status": row["operator_status"],
        "key_prefix": row["key_prefix"],
        "last_used_at": row["last_used_at"],
    }


async def create_operator_key(conn, operator_id: UUID, key_name: str = "default") -> str:
    """Generate and store an additional key for an existing operator. Returns plaintext key."""
    key = generate_api_key()
    key_hash = hash_key(key)
    key_prefix = key[:16]

    await conn.execute(
        """
        INSERT INTO api_keys (operator_id, key_hash, key_prefix, name, is_active)
        VALUES ($1, $2, $3, $4, true)
        """,
        operator_id,
        key_hash,
        key_prefix,
        key_name,
    )
    return key


async def create_agent_key(conn, agent_id: UUID) -> str:
    """Generate an agent key, store hash on agents row, return plaintext key."""
    key = generate_agent_key()
    key_hash_val = hash_key(key)
    key_prefix = key[:16]

    await conn.execute(
        "UPDATE agents SET key_hash = $1, key_prefix = $2 WHERE id = $3",
        key_hash_val,
        key_prefix,
        agent_id,
    )
    return key


async def validate_agent_key(conn, key: str) -> dict | None:
    """
    Look up agent key by SHA-256 hash.
    Returns agent dict if key is valid and agent is active, else None.
    """
    key_hash_val = hash_key(key)
    row = await conn.fetchrow(
        """
        SELECT a.id AS agent_id, a.operator_id, a.name, a.status,
               o.name AS operator_name, o.status AS operator_status
        FROM agents a
        JOIN operators o ON o.id = a.operator_id
        WHERE a.key_hash = $1
        """,
        key_hash_val,
    )
    if row is None:
        return None
    if row["status"] != "active":
        return None
    if row["operator_status"] != "active":
        return None
    return {
        "agent_id": row["agent_id"],
        "operator_id": row["operator_id"],
        "agent_name": row["name"],
        "operator_name": row["operator_name"],
    }


async def get_or_create_local_operator(conn) -> UUID:
    """Get or create the 'local' operator for auth-disabled mode.
    Returns the operator UUID."""
    row = await conn.fetchrow(
        "SELECT id FROM operators WHERE name = 'local'"
    )
    if row:
        return row["id"]

    row = await conn.fetchrow(
        """
        INSERT INTO operators (name, email, username, org)
        VALUES ('local', NULL, 'local', 'mnemo')
        ON CONFLICT (name) DO UPDATE SET name = 'local'
        RETURNING id
        """,
    )
    return row["id"]
