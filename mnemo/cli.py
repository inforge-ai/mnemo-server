"""
Mnemo CLI — manage operators and agents.

Commands:
  mnemo register-operator <name>   Create operator, print API key once.
  mnemo create-agent <name>        Create agent under authenticated operator.
  mnemo list-agents                List agents for authenticated operator.
  mnemo new-key                    Generate additional API key for operator.
  mnemo whoami                     Verify API key and show operator info.
"""

import asyncio
import os
import sys

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


if __name__ == "__main__":
    cli()
