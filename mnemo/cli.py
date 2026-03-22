"""
Mnemo CLI — manage operators and agents.

Commands:
  mnemo register-operator <name>   Create operator, print API key once.
  mnemo create-agent <name>        Create agent under authenticated operator.
  mnemo reactivate-agent <id>      Reactivate a departed agent.
  mnemo list-agents                List agents for authenticated operator.
  mnemo new-key                    Generate additional API key for operator.
  mnemo whoami                     Verify API key and show operator info.
  mnemo admin trust ...            Trust management (direct DB).
"""

import asyncio
import os
import sys
from uuid import UUID

import asyncpg
import click
import httpx


BASE_URL = "http://localhost:8000"


def _run(coro):
    return asyncio.run(coro)


def _api_key_from_env():
    key = os.environ.get("MNEMO_API_KEY", "")
    if not key:
        click.echo("MNEMO_API_KEY environment variable not set.", err=True)
        sys.exit(1)
    return key


def _database_url():
    url = os.environ.get("MNEMO_DATABASE_URL", "")
    if not url:
        click.echo("MNEMO_DATABASE_URL environment variable not set.", err=True)
        sys.exit(1)
    return url


async def _resolve_agent(conn: asyncpg.Connection, identifier: str) -> UUID:
    """Resolve an agent address (e.g. astraea:tom.inforge) or UUID string to a UUID."""
    try:
        return UUID(identifier)
    except ValueError:
        pass
    row = await conn.fetchrow(
        "SELECT agent_id FROM agent_addresses WHERE address = $1",
        identifier.lower(),
    )
    if not row:
        click.echo(f"Agent not found: {identifier}", err=True)
        sys.exit(1)
    return row["agent_id"]


async def _agent_display(conn: asyncpg.Connection, agent_id: UUID) -> str:
    """Return 'address (uuid)' for display, falling back to just the UUID."""
    row = await conn.fetchrow(
        "SELECT address FROM agent_addresses WHERE agent_id = $1", agent_id,
    )
    addr = row["address"] if row else None
    if addr:
        return f"{addr} ({agent_id})"
    return str(agent_id)


# ── Commands ───────────────────────────────────────────────────────────────────

@click.group()
@click.option("--base-url", default=BASE_URL, envvar="MNEMO_BASE_URL", help="Mnemo server URL.")
@click.pass_context
def cli(ctx, base_url):
    """Mnemo memory server management CLI."""
    ctx.ensure_object(dict)
    ctx.obj["base_url"] = base_url.rstrip("/")


@cli.command("register-operator")
@click.argument("name")
@click.option("--email", default=None, help="Operator email address.")
@click.pass_context
def register_operator(ctx, name, email):
    """Create an operator and print the API key once."""
    _run(_register_operator(ctx.obj["base_url"], name, email))


async def _register_operator(base_url, name, email):
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        body = {"name": name}
        if email:
            body["email"] = email
        resp = await client.post("/v1/auth/register-operator", json=body)
    if resp.status_code == 409:
        click.echo(f"Operator '{name}' already exists.", err=True)
        sys.exit(1)
    if resp.status_code not in (200, 201):
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)
    data = resp.json()
    click.echo(f"\nOperator: {data['name']}")
    click.echo(f"ID      : {data['operator_id']}")
    click.echo(f"API Key : {data['api_key']}")
    click.echo()
    click.echo("Save this key — it will not be shown again.")
    click.echo(f"  export MNEMO_API_KEY={data['api_key']}")
    click.echo()


@cli.command("create-agent")
@click.argument("name")
@click.option("--persona", default="", help="Agent persona description.")
@click.option("--tags", default="", help="Comma-separated domain tags.")
@click.pass_context
def create_agent(ctx, name, persona, tags):
    """Create an agent under the authenticated operator."""
    api_key = _api_key_from_env()
    domain_tags = [t.strip() for t in tags.split(",") if t.strip()]
    _run(_create_agent(ctx.obj["base_url"], api_key, name, persona, domain_tags))


async def _create_agent(base_url, api_key, name, persona, domain_tags):
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as client:
        resp = await client.post("/v1/agents", json={
            "name": name,
            "persona": persona,
            "domain_tags": domain_tags,
        })
    if resp.status_code == 409:
        click.echo(f"Agent '{name}' already exists under this operator.", err=True)
        sys.exit(1)
    if resp.status_code not in (200, 201):
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)
    data = resp.json()
    click.echo(f"\nAgent   : {data['name']}")
    click.echo(f"ID      : {data['id']}")
    click.echo()


@cli.command("reactivate-agent")
@click.argument("agent_id")
@click.pass_context
def reactivate_agent(ctx, agent_id):
    """Reactivate a departed agent (by UUID or address)."""
    api_key = _api_key_from_env()
    _run(_reactivate_agent(ctx.obj["base_url"], api_key, agent_id))


async def _reactivate_agent(base_url, api_key, agent_id):
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as client:
        resp = await client.post(f"/v1/agents/{agent_id}/reactivate")
    if resp.status_code == 404:
        click.echo("Agent not found.", err=True)
        sys.exit(1)
    if resp.status_code == 409:
        click.echo("Agent is already active.", err=True)
        sys.exit(1)
    if resp.status_code not in (200, 201):
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)
    data = resp.json()
    click.echo(f"\nReactivated: {data['name']}")
    click.echo(f"ID         : {data['id']}")
    click.echo(f"\nNote: previously revoked capabilities must be re-granted.")
    click.echo()


@cli.command("list-agents")
@click.pass_context
def list_agents(ctx):
    """List all agents for the authenticated operator."""
    api_key = _api_key_from_env()
    _run(_list_agents(ctx.obj["base_url"], api_key))


async def _list_agents(base_url, api_key):
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as client:
        resp = await client.get("/v1/agents")
    if resp.status_code != 200:
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)

    agents = resp.json()
    if not agents:
        click.echo("No active agents.")
        return

    click.echo(f"\n{'Name':<25} {'ID':<38} {'Persona'}")
    click.echo("-" * 80)
    for a in agents:
        persona = a.get("persona") or "(none)"
        click.echo(f"{a['name']:<25} {str(a['id']):<38} {persona}")
    click.echo()


@cli.command("new-key")
@click.pass_context
def new_key(ctx):
    """Generate an additional API key for the authenticated operator."""
    api_key = _api_key_from_env()
    _run(_new_key(ctx.obj["base_url"], api_key))


async def _new_key(base_url, api_key):
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as client:
        resp = await client.post("/v1/auth/new-key")
    if resp.status_code not in (200, 201):
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)
    data = resp.json()
    click.echo(f"\nNew API Key: {data['api_key']}")
    click.echo()
    click.echo("Save this key — it will not be shown again.")
    click.echo()


@cli.command()
@click.pass_context
def whoami(ctx):
    """Verify API key and show operator info."""
    api_key = _api_key_from_env()
    _run(_whoami(ctx.obj["base_url"], api_key))


async def _whoami(base_url, api_key):
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as client:
        resp = await client.get("/v1/auth/me")
    if resp.status_code == 401:
        click.echo("Invalid or inactive API key.", err=True)
        sys.exit(1)
    if resp.status_code != 200:
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)
    data = resp.json()
    click.echo(f"\nOperator: {data.get('name', '')}")
    click.echo(f"ID      : {data.get('id', '')}")
    click.echo(f"Email   : {data.get('email') or '(none)'}")
    click.echo(f"Agents  : {data.get('agent_count', 0)}")
    click.echo(f"Key     : {data.get('key_prefix', '')}...")
    click.echo()


# ── Admin commands (direct DB access) ─────────────────────────────────────────

@cli.group()
@click.pass_context
def admin(ctx):
    """Administrative commands (direct database access)."""
    pass


@admin.group()
@click.pass_context
def trust(ctx):
    """Manage agent trust relationships."""
    pass


@trust.command("list")
@click.option("--agent", required=True, help="Agent address or UUID.")
def trust_list(agent):
    """List an agent's trusted senders."""
    _run(_trust_list(agent))


async def _trust_list(agent_identifier):
    conn = await asyncpg.connect(_database_url())
    try:
        agent_id = await _resolve_agent(conn, agent_identifier)
        display = await _agent_display(conn, agent_id)

        rows = await conn.fetch("""
            SELECT t.trusted_sender_uuid, t.created_at, t.note,
                   aa.address AS sender_address
            FROM agent_trust t
            LEFT JOIN agent_addresses aa ON aa.agent_id = t.trusted_sender_uuid
            WHERE t.agent_uuid = $1
            ORDER BY t.created_at
        """, agent_id)

        click.echo(f"\nTrusted senders for {display}:")
        if not rows:
            click.echo("  (none)")
        else:
            click.echo(f"\n  {'Sender':<40} {'Since':<22} {'Note'}")
            click.echo("  " + "-" * 80)
            for r in rows:
                sender = r["sender_address"] or str(r["trusted_sender_uuid"])
                since = r["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                note = r["note"] or ""
                click.echo(f"  {sender:<40} {since:<22} {note}")
        click.echo()
    finally:
        await conn.close()


@trust.command("add")
@click.option("--agent", required=True, help="Agent address or UUID (the one granting trust).")
@click.option("--trusts", required=True, help="Sender address or UUID to trust.")
@click.option("--mutual", is_flag=True, default=False, help="Create trust in both directions.")
@click.option("--note", default=None, help="Optional note for the trust relationship.")
def trust_add(agent, trusts, mutual, note):
    """Add a trust relationship (unidirectional by default)."""
    _run(_trust_add(agent, trusts, mutual, note))


async def _trust_add(agent_identifier, sender_identifier, mutual, note):
    conn = await asyncpg.connect(_database_url())
    try:
        agent_id = await _resolve_agent(conn, agent_identifier)
        sender_id = await _resolve_agent(conn, sender_identifier)

        if agent_id == sender_id:
            click.echo("An agent cannot trust itself.", err=True)
            sys.exit(1)

        agent_display = await _agent_display(conn, agent_id)
        sender_display = await _agent_display(conn, sender_id)

        # Insert forward trust
        await conn.execute("""
            INSERT INTO agent_trust (agent_uuid, trusted_sender_uuid, note)
            VALUES ($1, $2, $3)
            ON CONFLICT (agent_uuid, trusted_sender_uuid) DO NOTHING
        """, agent_id, sender_id, note)
        click.echo(f"\n  {agent_display} now trusts {sender_display}")

        if mutual:
            await conn.execute("""
                INSERT INTO agent_trust (agent_uuid, trusted_sender_uuid, note)
                VALUES ($1, $2, $3)
                ON CONFLICT (agent_uuid, trusted_sender_uuid) DO NOTHING
            """, sender_id, agent_id, note)
            click.echo(f"  {sender_display} now trusts {agent_display}")

        click.echo()
    finally:
        await conn.close()


@trust.command("remove")
@click.option("--agent", required=True, help="Agent address or UUID.")
@click.option("--trusts", required=True, help="Sender address or UUID to remove from trust list.")
def trust_remove(agent, trusts):
    """Remove a single trust relationship."""
    _run(_trust_remove(agent, trusts))


async def _trust_remove(agent_identifier, sender_identifier):
    conn = await asyncpg.connect(_database_url())
    try:
        agent_id = await _resolve_agent(conn, agent_identifier)
        sender_id = await _resolve_agent(conn, sender_identifier)

        agent_display = await _agent_display(conn, agent_id)
        sender_display = await _agent_display(conn, sender_id)

        result = await conn.execute("""
            DELETE FROM agent_trust
            WHERE agent_uuid = $1 AND trusted_sender_uuid = $2
        """, agent_id, sender_id)

        count = int(result.split()[-1])
        if count:
            click.echo(f"\n  Removed: {agent_display} no longer trusts {sender_display}")
        else:
            click.echo(f"\n  No trust relationship found from {agent_display} to {sender_display}.")
        click.echo()
    finally:
        await conn.close()


@trust.command("revoke")
@click.option("--agent", required=True, help="Agent address or UUID.")
@click.confirmation_option(prompt="This will remove ALL trust rows involving this agent in both directions. Continue?")
def trust_revoke(agent):
    """Remove ALL trust rows involving this agent (both directions)."""
    _run(_trust_revoke(agent))


async def _trust_revoke(agent_identifier):
    conn = await asyncpg.connect(_database_url())
    try:
        agent_id = await _resolve_agent(conn, agent_identifier)
        agent_display = await _agent_display(conn, agent_id)

        result = await conn.execute("""
            DELETE FROM agent_trust
            WHERE agent_uuid = $1 OR trusted_sender_uuid = $1
        """, agent_id)

        count = int(result.split()[-1])
        click.echo(f"\n  Revoked {count} trust relationship(s) involving {agent_display}.")
        click.echo()
    finally:
        await conn.close()


@trust.command("inbox")
@click.option("--agent", required=True, help="Agent address or UUID.")
def trust_inbox(agent):
    """List capabilities/shared views from untrusted senders."""
    _run(_trust_inbox(agent))


async def _trust_inbox(agent_identifier):
    conn = await asyncpg.connect(_database_url())
    try:
        agent_id = await _resolve_agent(conn, agent_identifier)
        agent_display = await _agent_display(conn, agent_id)

        rows = await conn.fetch("""
            SELECT c.id AS cap_id, c.grantor_id, c.permissions, c.created_at,
                   v.name AS view_name, v.description AS view_desc,
                   aa.address AS grantor_address
            FROM capabilities c
            JOIN views v ON v.id = c.view_id
            LEFT JOIN agent_addresses aa ON aa.agent_id = c.grantor_id
            WHERE c.grantee_id = $1
              AND c.revoked = false
              AND (c.expires_at IS NULL OR c.expires_at > now())
              AND c.grantor_id NOT IN (
                  SELECT trusted_sender_uuid
                  FROM agent_trust
                  WHERE agent_uuid = $1
              )
            ORDER BY c.created_at DESC
        """, agent_id)

        click.echo(f"\nUntrusted inbox for {agent_display}:")
        if not rows:
            click.echo("  (empty)")
        else:
            click.echo(f"\n  {'From':<35} {'View':<25} {'Permissions':<18} {'Shared'}")
            click.echo("  " + "-" * 100)
            for r in rows:
                grantor = r["grantor_address"] or str(r["grantor_id"])
                view = r["view_name"] or "(unnamed)"
                perms = ", ".join(r["permissions"])
                shared = r["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                click.echo(f"  {grantor:<35} {view:<25} {perms:<18} {shared}")
        click.echo()
    finally:
        await conn.close()


if __name__ == "__main__":
    cli()
