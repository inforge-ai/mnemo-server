"""
Mnemo CLI — manage operators and agents.

Operator commands (require MNEMO_API_KEY / X-Operator-Key):
  mnemo create-agent <name>        Create agent under authenticated operator.
  mnemo list-agents                List agents for authenticated operator.
  mnemo new-key                    Generate additional API key for operator.
  mnemo whoami                     Verify API key and show operator info.

Admin commands (require MNEMO_ADMIN_TOKEN / X-Admin-Key):
  mnemo admin operator ...         Operator management.
  mnemo admin agent ...            Agent management.
  mnemo admin trust ...            Trust/sharing management.
"""

import asyncio
import json as json_mod
import os
import sys

import click
import httpx


BASE_URL = "http://localhost:8000"


def _run(coro):
    return asyncio.run(coro)


def _operator_key_from_env():
    key = os.environ.get("MNEMO_API_KEY", "")
    if not key:
        click.echo("MNEMO_API_KEY environment variable not set.", err=True)
        sys.exit(1)
    return key


def _admin_token(ctx):
    """Get admin token from click context, env, or abort."""
    token = ctx.obj.get("admin_token") or os.environ.get("MNEMO_ADMIN_TOKEN", "")
    if not token:
        click.echo("MNEMO_ADMIN_TOKEN not set. Pass --admin-token or set the env var.", err=True)
        sys.exit(1)
    return token


def _operator_headers(api_key):
    """Return headers for operator-level requests."""
    return {"X-Operator-Key": api_key}


async def _admin_request(base_url, token, method, path, json=None, params=None):
    """Send an admin API request and return the response, exiting on error."""
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        kwargs = {"headers": {"X-Admin-Key": token}}
        if json is not None:
            kwargs["json"] = json
        if params is not None:
            kwargs["params"] = params
        resp = await getattr(client, method)(path, **kwargs)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)
    return resp


# ── Operator commands ─────────────────────────────────────────────────────────

@click.group()
@click.option("--base-url", default=BASE_URL, envvar="MNEMO_BASE_URL", help="Mnemo server URL.")
@click.pass_context
def cli(ctx, base_url):
    """Mnemo memory server management CLI."""
    ctx.ensure_object(dict)
    ctx.obj["base_url"] = base_url.rstrip("/")


@cli.command("create-agent")
@click.argument("name")
@click.option("--persona", default="", help="Agent persona description.")
@click.option("--tags", default="", help="Comma-separated domain tags.")
@click.pass_context
def create_agent(ctx, name, persona, tags):
    """Create an agent under the authenticated operator."""
    api_key = _operator_key_from_env()
    domain_tags = [t.strip() for t in tags.split(",") if t.strip()]
    _run(_create_agent(ctx.obj["base_url"], api_key, name, persona, domain_tags))


async def _create_agent(base_url, api_key, name, persona, domain_tags):
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0,
        headers=_operator_headers(api_key),
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
    click.echo(f"\nAgent    : {data['name']}")
    click.echo(f"ID       : {data['id']}")
    if data.get("address"):
        click.echo(f"Address  : {data['address']}")
    agent_key = data.get("agent_key")
    if agent_key:
        click.echo(f"Agent Key: {agent_key}")
        click.echo()
        click.echo("Save this key — it will not be shown again.")
        click.echo(f"  export MNEMO_AGENT_KEY={agent_key}")
    click.echo()


@cli.command("list-agents")
@click.pass_context
def list_agents(ctx):
    """List all agents for the authenticated operator."""
    api_key = _operator_key_from_env()
    _run(_list_agents(ctx.obj["base_url"], api_key))


async def _list_agents(base_url, api_key):
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0,
        headers=_operator_headers(api_key),
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


@cli.command("rotate-agent-key")
@click.argument("agent_id")
@click.pass_context
def rotate_agent_key(ctx, agent_id):
    """Rotate an agent's key. Returns the new key once."""
    api_key = _operator_key_from_env()
    _run(_rotate_agent_key(ctx.obj["base_url"], api_key, agent_id))


async def _rotate_agent_key(base_url, api_key, agent_id):
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0,
        headers=_operator_headers(api_key),
    ) as client:
        resp = await client.post(f"/v1/agents/{agent_id}/rotate-key")
    if resp.status_code == 404:
        click.echo("Agent not found.", err=True)
        sys.exit(1)
    if resp.status_code == 403:
        click.echo("Agent not owned by this operator.", err=True)
        sys.exit(1)
    if resp.status_code not in (200, 201):
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)
    data = resp.json()
    click.echo(f"\nAgent    : {data.get('name', '')}")
    click.echo(f"ID       : {data['agent_id']}")
    if data.get("address"):
        click.echo(f"Address  : {data['address']}")
    click.echo(f"Agent Key: {data['agent_key']}")
    click.echo()
    click.echo("Save this key — it will not be shown again. The previous key is now invalid.")
    click.echo(f"  export MNEMO_AGENT_KEY={data['agent_key']}")
    click.echo()


@cli.command("new-key")
@click.pass_context
def new_key(ctx):
    """Generate an additional API key for the authenticated operator."""
    api_key = _operator_key_from_env()
    _run(_new_key(ctx.obj["base_url"], api_key))


async def _new_key(base_url, api_key):
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0,
        headers=_operator_headers(api_key),
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
    api_key = _operator_key_from_env()
    _run(_whoami(ctx.obj["base_url"], api_key))


async def _whoami(base_url, api_key):
    async with httpx.AsyncClient(
        base_url=base_url, timeout=30.0,
        headers=_operator_headers(api_key),
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
    click.echo(f"Role    : {data.get('role', '')}")
    click.echo(f"Agents  : {data.get('agent_count', 0)}")
    click.echo()


# ── Admin commands (API-based) ─────────────────────────────────────────────────

@cli.group()
@click.option("--admin-token", envvar="MNEMO_ADMIN_TOKEN", default=None, help="Admin token.")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output raw JSON.")
@click.pass_context
def admin(ctx, admin_token, output_json):
    """Administrative commands (requires admin token)."""
    ctx.ensure_object(dict)
    ctx.obj["admin_token"] = admin_token
    ctx.obj["json"] = output_json


# ── Admin: operator subgroup ──────────────────────────────────────────────────

@admin.group()
@click.pass_context
def operator(ctx):
    """Manage operators."""
    pass


@operator.command("create")
@click.option("--username", required=True, help="Operator username (lowercase, a-z0-9-).")
@click.option("--org", required=True, help="Organization slug (lowercase, a-z0-9-).")
@click.option("--display-name", required=True, help="Display name for the operator.")
@click.option("--email", required=True, help="Operator email address.")
@click.pass_context
def operator_create(ctx, username, org, display_name, email):
    """Create a new operator."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_operator_create(base_url, token, username, org, display_name, email, ctx.obj["json"]))


async def _operator_create(base_url, token, username, org, display_name, email, output_json):
    resp = await _admin_request(base_url, token, "post", "/v1/admin/operators", json={
        "username": username,
        "org": org,
        "display_name": display_name,
        "email": email,
    })
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    click.echo(f"\nOperator created:")
    click.echo(f"  UUID        : {data['uuid']}")
    click.echo(f"  Username    : {data['username']}")
    click.echo(f"  Org         : {data['org']}")
    click.echo(f"  Display Name: {data['display_name']}")
    click.echo(f"  Email       : {data['email']}")
    click.echo(f"  API Key     : {data['api_key']}")
    click.echo()
    click.echo("Save this key — it will not be shown again.")
    click.echo(f"  export MNEMO_API_KEY={data['api_key']}")
    click.echo()


@operator.command("list")
@click.pass_context
def operator_list(ctx):
    """List all operators."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_operator_list(base_url, token, ctx.obj["json"]))


async def _operator_list(base_url, token, output_json):
    resp = await _admin_request(base_url, token, "get", "/v1/admin/operators")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    operators = data.get("operators", [])
    if not operators:
        click.echo("No operators.")
        return
    click.echo(f"\n{'UUID':<38} {'Username':<20} {'Org':<15} {'Status':<12} {'Agents':<8} {'Email'}")
    click.echo("-" * 120)
    for op in operators:
        click.echo(
            f"{op['uuid']:<38} {op['username']:<20} {op['org']:<15} "
            f"{op['status']:<12} {op['agent_count']:<8} {op.get('email') or ''}"
        )
    click.echo()


@operator.command("show")
@click.argument("operator_id")
@click.pass_context
def operator_show(ctx, operator_id):
    """Show details for a single operator."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_operator_show(base_url, token, operator_id, ctx.obj["json"]))


async def _operator_show(base_url, token, operator_id, output_json):
    resp = await _admin_request(base_url, token, "get", f"/v1/admin/operators/{operator_id}")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    click.echo(f"\nOperator: {data['display_name']}")
    click.echo(f"  UUID    : {data['uuid']}")
    click.echo(f"  Username: {data['username']}")
    click.echo(f"  Org     : {data['org']}")
    click.echo(f"  Email   : {data.get('email') or '(none)'}")
    click.echo(f"  Status  : {data['status']}")
    agents = data.get("agents", [])
    if agents:
        click.echo(f"\n  Agents ({len(agents)}):")
        for a in agents:
            addr = a.get("address") or "(no address)"
            click.echo(f"    {a['name']:<20} {addr:<35} {a['status']}")
    else:
        click.echo("\n  Agents: (none)")
    click.echo()


@operator.command("suspend")
@click.argument("operator_id")
@click.pass_context
def operator_suspend(ctx, operator_id):
    """Suspend an operator and depart all their agents."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_operator_suspend(base_url, token, operator_id, ctx.obj["json"]))


async def _operator_suspend(base_url, token, operator_id, output_json):
    resp = await _admin_request(base_url, token, "post", f"/v1/admin/operators/{operator_id}/suspend")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    click.echo(f"\nSuspended operator {data['username']} ({data['uuid']})")
    click.echo(f"  Agents departed: {data.get('agents_departed', 0)}")
    click.echo()


@operator.command("reinstate")
@click.argument("operator_id")
@click.pass_context
def operator_reinstate(ctx, operator_id):
    """Reinstate a suspended operator."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_operator_reinstate(base_url, token, operator_id, ctx.obj["json"]))


async def _operator_reinstate(base_url, token, operator_id, output_json):
    resp = await _admin_request(base_url, token, "post", f"/v1/admin/operators/{operator_id}/reinstate")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    click.echo(f"\nReinstated operator {data['username']} ({data['uuid']})")
    click.echo(f"  Status: {data['status']}")
    if data.get("note"):
        click.echo(f"  Note  : {data['note']}")
    click.echo()


@operator.command("rotate-key")
@click.argument("operator_id")
@click.pass_context
def operator_rotate_key(ctx, operator_id):
    """Rotate API keys for an operator (deactivate old, issue new)."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_operator_rotate_key(base_url, token, operator_id, ctx.obj["json"]))


async def _operator_rotate_key(base_url, token, operator_id, output_json):
    resp = await _admin_request(base_url, token, "post", f"/v1/admin/operators/{operator_id}/rotate-key")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    click.echo(f"\nRotated keys for {data['username']} ({data['uuid']})")
    click.echo(f"  New API Key: {data['api_key']}")
    click.echo()
    click.echo("Save this key — it will not be shown again.")
    click.echo()


@operator.command("set-sharing-scope")
@click.argument("operator_id")
@click.argument("scope", type=click.Choice(["none", "intra", "full"]))
@click.pass_context
def operator_set_sharing_scope(ctx, operator_id, scope):
    """Set the sharing scope for an operator (none/intra/full)."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_operator_set_sharing_scope(base_url, token, operator_id, scope, ctx.obj["json"]))


async def _operator_set_sharing_scope(base_url, token, operator_id, scope, output_json):
    resp = await _admin_request(
        base_url, token, "patch",
        f"/v1/admin/operators/{operator_id}/sharing-scope",
        json={"sharing_scope": scope},
    )
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    click.echo(f"\nUpdated sharing scope for {data['username']}:")
    click.echo(f"  Scope: {data['sharing_scope']}")
    click.echo()


# ── Admin: agent subgroup ─────────────────────────────────────────────────────

@admin.group()
@click.pass_context
def agent(ctx):
    """Manage agents."""
    pass


@agent.command("list")
@click.option("--operator", "operator_id", default=None, help="Filter by operator UUID.")
@click.option("--status", default="active", type=click.Choice(["active", "departed", "all"]), help="Filter by status (default: active).")
@click.pass_context
def agent_list(ctx, operator_id, status):
    """List all agents."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_agent_list(base_url, token, operator_id, status, ctx.obj["json"]))


async def _agent_list(base_url, token, operator_id, status, output_json):
    params = {}
    if operator_id:
        params["operator"] = operator_id
    if status and status != "all":
        params["status"] = status
    resp = await _admin_request(base_url, token, "get", "/v1/admin/agents", params=params or None)
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    agents = data.get("agents", [])
    if not agents:
        click.echo("No agents found.")
        return
    click.echo(f"\n{'UUID':<38} {'Address':<30} {'Name':<20} {'Status':<12} {'Operator'}")
    click.echo("-" * 130)
    for a in agents:
        addr = a.get("address") or "(none)"
        click.echo(
            f"{a['uuid']:<38} {addr:<30} {a['display_name']:<20} "
            f"{a['status']:<12} {a.get('operator_username', '')}"
        )
    click.echo()


@agent.command("depart")
@click.argument("agent_id")
@click.pass_context
def agent_depart(ctx, agent_id):
    """Force-depart an agent."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_agent_depart(base_url, token, agent_id, ctx.obj["json"]))


async def _agent_depart(base_url, token, agent_id, output_json):
    resp = await _admin_request(base_url, token, "post", f"/v1/admin/agents/{agent_id}/depart")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    addr = data.get("address") or data.get("uuid", agent_id)
    click.echo(f"\nDeparted agent: {addr}")
    click.echo(f"  UUID                 : {data.get('uuid', agent_id)}")
    click.echo(f"  Capabilities revoked : {data.get('capabilities_revoked', 0)}")
    click.echo(f"  Data expires at      : {data.get('data_expires_at', 'N/A')}")
    click.echo()


@agent.command("reinstate")
@click.argument("agent_id")
@click.pass_context
def agent_reinstate(ctx, agent_id):
    """Reinstate a departed agent."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_agent_reinstate(base_url, token, agent_id, ctx.obj["json"]))


async def _agent_reinstate(base_url, token, agent_id, output_json):
    resp = await _admin_request(base_url, token, "post", f"/v1/admin/agents/{agent_id}/reinstate")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    addr = data.get("address") or data.get("uuid", agent_id)
    click.echo(f"\nReinstated agent: {addr}")
    click.echo(f"  UUID   : {data.get('uuid', agent_id)}")
    click.echo(f"  Status : {data.get('status', 'active')}")
    if data.get("message"):
        click.echo(f"  Note   : {data['message']}")
    click.echo()


@agent.command("rotate-key")
@click.argument("agent_id")
@click.pass_context
def agent_rotate_key(ctx, agent_id):
    """Rotate an agent's key (returns new key once)."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_agent_rotate_key(base_url, token, agent_id, ctx.obj["json"]))


async def _agent_rotate_key(base_url, token, agent_id, output_json):
    resp = await _admin_request(base_url, token, "post", f"/v1/admin/agents/{agent_id}/rotate-key")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    click.echo(f"\nRotated key for agent: {data.get('name', agent_id)}")
    click.echo(f"  ID       : {data['agent_id']}")
    if data.get("address"):
        click.echo(f"  Address  : {data['address']}")
    click.echo(f"  Agent Key: {data['agent_key']}")
    click.echo()
    click.echo("Save this key — it will not be shown again. The previous key is now invalid.")
    click.echo()


# ── Admin: trust subgroup ─────────────────────────────────────────────────────

@admin.group()
@click.pass_context
def trust(ctx):
    """Manage trust and sharing."""
    pass


@trust.command("status")
@click.pass_context
def trust_status(ctx):
    """Show current sharing enabled/disabled status."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_trust_status(base_url, token, ctx.obj["json"]))


async def _trust_status(base_url, token, output_json):
    resp = await _admin_request(base_url, token, "get", "/v1/admin/trust/status")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    enabled = data.get("sharing_enabled", False)
    click.echo(f"\nSharing: {'ENABLED' if enabled else 'DISABLED'}")
    click.echo()


@trust.command("disable")
@click.pass_context
def trust_disable(ctx):
    """Disable sharing globally."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_trust_disable(base_url, token, ctx.obj["json"]))


async def _trust_disable(base_url, token, output_json):
    resp = await _admin_request(base_url, token, "post", "/v1/admin/trust/disable")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    click.echo(f"\nSharing: DISABLED")
    if data.get("note"):
        click.echo(f"  Note: {data['note']}")
    click.echo()


@trust.command("enable")
@click.pass_context
def trust_enable(ctx):
    """Enable sharing globally."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_trust_enable(base_url, token, ctx.obj["json"]))


async def _trust_enable(base_url, token, output_json):
    resp = await _admin_request(base_url, token, "post", "/v1/admin/trust/enable")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    click.echo(f"\nSharing: ENABLED")
    click.echo()


@trust.command("list")
@click.option("--operator", "operator_id", default=None, help="Filter by operator UUID.")
@click.option("--agent", "agent_id", default=None, help="Filter by agent UUID.")
@click.pass_context
def trust_list(ctx, operator_id, agent_id):
    """List active shares/capabilities."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_trust_list(base_url, token, operator_id, agent_id, ctx.obj["json"]))


async def _trust_list(base_url, token, operator_id, agent_id, output_json):
    params = {}
    if operator_id:
        params["operator"] = operator_id
    if agent_id:
        params["agent"] = agent_id
    resp = await _admin_request(base_url, token, "get", "/v1/admin/trust/shares", params=params or None)
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    shares = data.get("shares", [])
    if not shares:
        click.echo("No active shares.")
        return
    click.echo(f"\n{'Capability ID':<38} {'Grantor':<28} {'Grantee':<28} {'View Name':<20} {'Created'}")
    click.echo("-" * 140)
    for s in shares:
        grantor = s.get("grantor_address") or "(unknown)"
        grantee = s.get("grantee_address") or "(unknown)"
        view = s.get("view_name") or "(unnamed)"
        created = str(s.get("created_at", ""))[:19]
        click.echo(f"{s['capability_id']:<38} {grantor:<28} {grantee:<28} {view:<20} {created}")
    click.echo()


@trust.command("revoke")
@click.argument("capability_id")
@click.pass_context
def trust_revoke(ctx, capability_id):
    """Revoke a capability (with cascade)."""
    token = _admin_token(ctx)
    base_url = ctx.obj["base_url"]
    _run(_trust_revoke(base_url, token, capability_id, ctx.obj["json"]))


async def _trust_revoke(base_url, token, capability_id, output_json):
    resp = await _admin_request(base_url, token, "delete", f"/v1/admin/trust/shares/{capability_id}")
    data = resp.json()
    if output_json:
        click.echo(json_mod.dumps(data, indent=2, default=str))
        return
    click.echo(f"\nRevoked capability: {data.get('capability_id', capability_id)}")
    click.echo(f"  Cascade count: {data.get('cascade_count', 0)}")
    click.echo()


if __name__ == "__main__":
    cli()
