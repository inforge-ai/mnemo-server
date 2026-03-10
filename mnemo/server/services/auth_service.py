import hashlib
import re
import secrets
from uuid import UUID


def generate_api_key() -> str:
    """Generate a new mnemo_ prefixed API key."""
    return "mnemo_" + secrets.token_urlsafe(32)


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
        INSERT INTO operators (name, email, username, org)
        VALUES ($1, $2, $3, $4)
        RETURNING id, name, email, username, org, created_at, is_active
        """,
        name,
        email,
        username,
        org,
    )

    operator = {
        "id": str(row["id"]),
        "name": row["name"],
        "email": row["email"],
        "username": row["username"],
        "org": row["org"],
        "created_at": row["created_at"],
        "is_active": row["is_active"],
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
               o.id AS operator_id, o.name, o.email, o.is_active
        FROM api_keys k
        JOIN operators o ON o.id = k.operator_id
        WHERE k.key_hash = $1 AND k.is_active = true
        """,
        key_hash,
    )
    if row is None:
        return None

    if not row["is_active"]:
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
        "is_active": row["is_active"],
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
