"""
Mnemo CLI — manage agents and API keys.

Commands:
  mnemo register <name>          Create agent (or add key to existing), print key once.
  mnemo new-key <name>           Generate additional key for existing agent.
  mnemo list-agents              List all active agents with key counts.
  mnemo whoami --api-key <key>   Verify a key and show agent info.
"""

import asyncio
import sys

import click
import httpx


BASE_URL = "http://localhost:8000"


def _run(coro):
    return asyncio.run(coro)


# ── Commands ───────────────────────────────────────────────────────────────────

@click.group()
@click.option("--base-url", default=BASE_URL, envvar="MNEMO_BASE_URL", help="Mnemo server URL.")
@click.pass_context
def cli(ctx, base_url):
    """Mnemo memory server management CLI."""
    ctx.ensure_object(dict)
    ctx.obj["base_url"] = base_url.rstrip("/")


@cli.command()
@click.argument("name")
@click.option("--persona", default="", help="Agent persona description.")
@click.option("--tags", default="", help="Comma-separated domain tags.")
@click.option("--key-name", default="default", help="Name for this key.")
@click.pass_context
def register(ctx, name, persona, tags, key_name):
    """Create agent (or add a key to existing agent), print key once."""
    domain_tags = [t.strip() for t in tags.split(",") if t.strip()]
    _run(_register(ctx.obj["base_url"], name, persona, domain_tags, key_name))


async def _register(base_url, name, persona, domain_tags, key_name):
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        resp = await client.post(
            "/v1/auth/register",
            json={"name": name, "persona": persona, "domain_tags": domain_tags, "key_name": key_name},
        )
    if resp.status_code not in (200, 201):
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)
    data = resp.json()
    _print_key_result(data["name"], data["agent_id"], data["api_key"])


@cli.command("new-key")
@click.argument("name")
@click.option("--key-name", default="default", help="Name for this key.")
@click.pass_context
def new_key(ctx, name, key_name):
    """Generate an additional key for an existing agent."""
    _run(_new_key(ctx.obj["base_url"], name, key_name))


async def _new_key(base_url, name, key_name):
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # Find agent by name first to confirm it exists
        resp = await client.get("/v1/agents", params={"name": name})
        if resp.status_code != 200 or not resp.json():
            click.echo(f"Agent '{name}' not found.", err=True)
            sys.exit(1)

        # Register (idempotent — just adds a new key)
        resp = await client.post(
            "/v1/auth/register",
            json={"name": name, "persona": "", "domain_tags": [], "key_name": key_name},
        )
    if resp.status_code not in (200, 201):
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)
    data = resp.json()
    _print_key_result(data["name"], data["agent_id"], data["api_key"])


@cli.command("list-agents")
@click.pass_context
def list_agents(ctx):
    """List all active agents with key counts and last_used_at."""
    _run(_list_agents(ctx.obj["base_url"]))


async def _list_agents(base_url):
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # Use the postgres directly via a custom endpoint — for now use the agents
        # list from the REST API and summarise what we can
        # We need a stats endpoint for this; fall back to fetching known agents
        # by querying with a wildcard. The /v1/agents requires exact name match,
        # so we'll use a direct DB query via a new endpoint when available.
        # For now, show a notice that this requires direct DB access.
        click.echo("Fetching agents... (requires DB access)")

        import asyncpg
        import os
        db_url = os.environ.get("MNEMO_DATABASE_URL", "postgresql://mnemo:mnemo@localhost:5432/mnemo")
        conn = await asyncpg.connect(db_url)
        rows = await conn.fetch(
            """
            SELECT a.id, a.name, a.persona, a.created_at,
                   COUNT(k.id) AS key_count,
                   MAX(k.last_used_at) AS last_used_at
            FROM agents a
            LEFT JOIN api_keys k ON k.agent_id = a.id AND k.is_active = true
            WHERE a.is_active = true
            GROUP BY a.id
            ORDER BY a.created_at ASC
            """
        )
        await conn.close()

    if not rows:
        click.echo("No active agents.")
        return

    click.echo(f"\n{'Name':<25} {'ID':<38} {'Keys':>4}  Last Used")
    click.echo("-" * 80)
    for r in rows:
        last = r["last_used_at"].strftime("%Y-%m-%d %H:%M") if r["last_used_at"] else "never"
        click.echo(f"{r['name']:<25} {str(r['id']):<38} {r['key_count']:>4}  {last}")
    click.echo()


@cli.command()
@click.option("--api-key", required=True, envvar="MNEMO_API_KEY", help="API key to verify.")
@click.pass_context
def whoami(ctx, api_key):
    """Verify an API key and show the associated agent info."""
    _run(_whoami(ctx.obj["base_url"], api_key))


async def _whoami(base_url, api_key):
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0, headers={"Authorization": f"Bearer {api_key}"}
    ) as client:
        resp = await client.get("/v1/auth/me")
    if resp.status_code == 401:
        click.echo("Invalid or inactive API key.", err=True)
        sys.exit(1)
    if resp.status_code != 200:
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)
    data = resp.json()
    agent_id = data.get("agent_id") or data.get("id", "")
    click.echo(f"\nAgent   : {data.get('name', '')}")
    click.echo(f"ID      : {agent_id}")
    click.echo(f"Persona : {data.get('persona', '') or '(none)'}")
    tags = data.get("domain_tags") or []
    click.echo(f"Tags    : {', '.join(tags) if tags else '(none)'}")
    click.echo(f"Key     : {data.get('key_prefix', '')}...")
    last = data.get("last_used_at")
    click.echo(f"Last use: {last or 'never'}")
    click.echo()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _print_key_result(name: str, agent_id: str, api_key: str):
    click.echo(f"\nAgent   : {name}")
    click.echo(f"ID      : {agent_id}")
    click.echo(f"API Key : {api_key}")
    click.echo()
    click.echo("Save this key — it will not be shown again.")
    click.echo("Add to your MCP config or service file:")
    click.echo(f"  MNEMO_API_KEY={api_key}")
    click.echo()


if __name__ == "__main__":
    cli()
