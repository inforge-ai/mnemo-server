"""
Tests for the mnemo CLI.

Uses Click's CliRunner + respx to mock HTTP responses. No real server needed.
"""

import json

import pytest
import respx
from click.testing import CliRunner
from httpx import Response

from mnemo.cli import cli

BASE = "http://localhost:8000"


@pytest.fixture
def runner():
    return CliRunner(env={"MNEMO_BASE_URL": BASE})


# ── Operator commands ────────────────────────────────────────────────────────

OPERATOR_ENV = {"MNEMO_API_KEY": "mnemo_opkey", "MNEMO_BASE_URL": BASE}


class TestCreateAgent:
    @respx.mock
    def test_success_shows_agent_key(self, runner):
        respx.post(f"{BASE}/v1/agents").mock(
            return_value=Response(201, json={
                "name": "bot", "id": "ag-uuid-1", "address": "bot:op.org",
                "agent_key": "mnemo_ag_secret123",
            })
        )
        result = runner.invoke(
            cli, ["create-agent", "bot", "--persona", "helper", "--tags", "py,ml"],
            env=OPERATOR_ENV,
        )
        assert result.exit_code == 0
        assert "bot" in result.output
        assert "mnemo_ag_secret123" in result.output
        assert "Save this key" in result.output

    @respx.mock
    def test_sends_x_operator_key_header(self, runner):
        route = respx.post(f"{BASE}/v1/agents").mock(
            return_value=Response(201, json={
                "name": "bot", "id": "ag-1", "agent_key": "mnemo_ag_k",
            })
        )
        runner.invoke(cli, ["create-agent", "bot"], env=OPERATOR_ENV)
        req = route.calls.last.request
        assert req.headers.get("x-operator-key") == "mnemo_opkey"
        assert "authorization" not in req.headers

    @respx.mock
    def test_conflict(self, runner):
        respx.post(f"{BASE}/v1/agents").mock(
            return_value=Response(409, text="conflict")
        )
        result = runner.invoke(cli, ["create-agent", "bot"], env=OPERATOR_ENV)
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_missing_api_key(self, runner):
        result = runner.invoke(
            cli, ["create-agent", "bot"],
            env={"MNEMO_API_KEY": "", "MNEMO_BASE_URL": BASE},
        )
        assert result.exit_code != 0
        assert "MNEMO_API_KEY" in result.output


class TestRotateAgentKey:
    @respx.mock
    def test_success(self, runner):
        respx.post(f"{BASE}/v1/agents/ag-1/rotate-key").mock(
            return_value=Response(200, json={
                "agent_id": "ag-1", "name": "bot", "address": "bot:op.org",
                "agent_key": "mnemo_ag_newkey",
                "message": "Save this key",
            })
        )
        result = runner.invoke(
            cli, ["rotate-agent-key", "ag-1"], env=OPERATOR_ENV,
        )
        assert result.exit_code == 0
        assert "mnemo_ag_newkey" in result.output
        assert "previous key is now invalid" in result.output

    @respx.mock
    def test_sends_x_operator_key_header(self, runner):
        route = respx.post(f"{BASE}/v1/agents/ag-1/rotate-key").mock(
            return_value=Response(200, json={
                "agent_id": "ag-1", "name": "bot",
                "agent_key": "mnemo_ag_k",
            })
        )
        runner.invoke(cli, ["rotate-agent-key", "ag-1"], env=OPERATOR_ENV)
        req = route.calls.last.request
        assert req.headers.get("x-operator-key") == "mnemo_opkey"

    @respx.mock
    def test_not_found(self, runner):
        respx.post(f"{BASE}/v1/agents/bad/rotate-key").mock(
            return_value=Response(404, text="not found")
        )
        result = runner.invoke(
            cli, ["rotate-agent-key", "bad"], env=OPERATOR_ENV,
        )
        assert result.exit_code != 0
        assert "not found" in result.output

    @respx.mock
    def test_not_owned(self, runner):
        respx.post(f"{BASE}/v1/agents/ag-1/rotate-key").mock(
            return_value=Response(403, text="not owned")
        )
        result = runner.invoke(
            cli, ["rotate-agent-key", "ag-1"], env=OPERATOR_ENV,
        )
        assert result.exit_code != 0
        assert "not owned" in result.output


class TestListAgents:
    @respx.mock
    def test_success(self, runner):
        respx.get(f"{BASE}/v1/agents").mock(
            return_value=Response(200, json=[
                {"name": "bot", "id": "ag-1", "persona": "helper"},
            ])
        )
        result = runner.invoke(cli, ["list-agents"], env=OPERATOR_ENV)
        assert result.exit_code == 0
        assert "bot" in result.output

    @respx.mock
    def test_sends_x_operator_key_header(self, runner):
        route = respx.get(f"{BASE}/v1/agents").mock(
            return_value=Response(200, json=[])
        )
        runner.invoke(cli, ["list-agents"], env=OPERATOR_ENV)
        req = route.calls.last.request
        assert req.headers.get("x-operator-key") == "mnemo_opkey"
        assert "authorization" not in req.headers

    @respx.mock
    def test_empty(self, runner):
        respx.get(f"{BASE}/v1/agents").mock(
            return_value=Response(200, json=[])
        )
        result = runner.invoke(cli, ["list-agents"], env=OPERATOR_ENV)
        assert result.exit_code == 0
        assert "No active agents" in result.output


class TestNewKey:
    @respx.mock
    def test_success(self, runner):
        respx.post(f"{BASE}/v1/auth/new-key").mock(
            return_value=Response(200, json={"api_key": "mnemo_newkey"})
        )
        result = runner.invoke(cli, ["new-key"], env=OPERATOR_ENV)
        assert result.exit_code == 0
        assert "mnemo_newkey" in result.output

    @respx.mock
    def test_sends_x_operator_key_header(self, runner):
        route = respx.post(f"{BASE}/v1/auth/new-key").mock(
            return_value=Response(200, json={"api_key": "mnemo_k"})
        )
        runner.invoke(cli, ["new-key"], env=OPERATOR_ENV)
        req = route.calls.last.request
        assert req.headers.get("x-operator-key") == "mnemo_opkey"


class TestWhoami:
    @respx.mock
    def test_success(self, runner):
        respx.get(f"{BASE}/v1/auth/me").mock(
            return_value=Response(200, json={
                "name": "acme", "id": "op-1", "role": "operator",
                "agent_count": 3,
            })
        )
        result = runner.invoke(cli, ["whoami"], env=OPERATOR_ENV)
        assert result.exit_code == 0
        assert "acme" in result.output
        assert "3" in result.output

    @respx.mock
    def test_invalid_key(self, runner):
        respx.get(f"{BASE}/v1/auth/me").mock(
            return_value=Response(401, text="unauthorized")
        )
        result = runner.invoke(cli, ["whoami"], env=OPERATOR_ENV)
        assert result.exit_code != 0
        assert "Invalid" in result.output

    @respx.mock
    def test_sends_x_operator_key_header(self, runner):
        route = respx.get(f"{BASE}/v1/auth/me").mock(
            return_value=Response(200, json={
                "name": "a", "id": "1", "role": "operator", "agent_count": 0,
            })
        )
        runner.invoke(cli, ["whoami"], env=OPERATOR_ENV)
        req = route.calls.last.request
        assert req.headers.get("x-operator-key") == "mnemo_opkey"


# ── Admin: operator commands ─────────────────────────────────────────────────

ADMIN_ENV = {"MNEMO_ADMIN_TOKEN": "secret", "MNEMO_BASE_URL": BASE}


class TestAdminOperator:
    @respx.mock
    def test_create(self, runner):
        respx.post(f"{BASE}/v1/admin/operators").mock(
            return_value=Response(201, json={
                "uuid": "op-1", "username": "jdoe", "org": "acme",
                "display_name": "Jane", "email": "j@a.com", "api_key": "mnemo_k",
            })
        )
        result = runner.invoke(cli, [
            "admin", "--admin-token", "secret",
            "operator", "create",
            "--username", "jdoe", "--org", "acme",
            "--display-name", "Jane", "--email", "j@a.com",
        ])
        assert result.exit_code == 0
        assert "mnemo_k" in result.output
        assert "export MNEMO_API_KEY" in result.output

    @respx.mock
    def test_sends_x_admin_key_header(self, runner):
        route = respx.post(f"{BASE}/v1/admin/operators").mock(
            return_value=Response(201, json={
                "uuid": "op-1", "username": "jdoe", "org": "acme",
                "display_name": "Jane", "email": "j@a.com", "api_key": "mnemo_k",
            })
        )
        runner.invoke(cli, [
            "admin", "--admin-token", "secret",
            "operator", "create",
            "--username", "jdoe", "--org", "acme",
            "--display-name", "Jane", "--email", "j@a.com",
        ])
        req = route.calls.last.request
        assert req.headers.get("x-admin-key") == "secret"

    @respx.mock
    def test_list(self, runner):
        respx.get(f"{BASE}/v1/admin/operators").mock(
            return_value=Response(200, json={"operators": [
                {"uuid": "op-1", "username": "jdoe", "org": "acme",
                 "status": "active", "agent_count": 2, "email": "j@a.com"},
            ]})
        )
        result = runner.invoke(cli, ["admin", "operator", "list"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "jdoe" in result.output

    @respx.mock
    def test_list_empty(self, runner):
        respx.get(f"{BASE}/v1/admin/operators").mock(
            return_value=Response(200, json={"operators": []})
        )
        result = runner.invoke(cli, ["admin", "operator", "list"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "No operators" in result.output

    @respx.mock
    def test_show(self, runner):
        respx.get(f"{BASE}/v1/admin/operators/op-1").mock(
            return_value=Response(200, json={
                "uuid": "op-1", "username": "jdoe", "org": "acme",
                "display_name": "Jane", "email": "j@a.com", "status": "active",
                "agents": [{"name": "bot", "address": "bot:jdoe.acme", "status": "active"}],
            })
        )
        result = runner.invoke(cli, ["admin", "operator", "show", "op-1"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "Jane" in result.output
        assert "bot" in result.output

    @respx.mock
    def test_suspend(self, runner):
        respx.post(f"{BASE}/v1/admin/operators/op-1/suspend").mock(
            return_value=Response(200, json={
                "uuid": "op-1", "username": "jdoe", "agents_departed": 2,
            })
        )
        result = runner.invoke(cli, ["admin", "operator", "suspend", "op-1"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "Suspended" in result.output

    @respx.mock
    def test_reinstate(self, runner):
        respx.post(f"{BASE}/v1/admin/operators/op-1/reinstate").mock(
            return_value=Response(200, json={
                "uuid": "op-1", "username": "jdoe", "status": "active",
            })
        )
        result = runner.invoke(cli, ["admin", "operator", "reinstate", "op-1"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "Reinstated" in result.output

    @respx.mock
    def test_rotate_key(self, runner):
        respx.post(f"{BASE}/v1/admin/operators/op-1/rotate-key").mock(
            return_value=Response(200, json={
                "uuid": "op-1", "username": "jdoe", "api_key": "mnemo_new",
            })
        )
        result = runner.invoke(cli, ["admin", "operator", "rotate-key", "op-1"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "mnemo_new" in result.output

    @respx.mock
    def test_json_output(self, runner):
        respx.get(f"{BASE}/v1/admin/operators").mock(
            return_value=Response(200, json={"operators": [
                {"uuid": "op-1", "username": "jdoe", "org": "acme",
                 "status": "active", "agent_count": 0, "email": ""},
            ]})
        )
        result = runner.invoke(cli, ["admin", "--json", "operator", "list"], env=ADMIN_ENV)
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "operators" in parsed

    def test_missing_admin_token(self, runner):
        result = runner.invoke(
            cli, ["admin", "operator", "list"],
            env={"MNEMO_ADMIN_TOKEN": "", "MNEMO_BASE_URL": BASE},
        )
        assert result.exit_code != 0
        assert "MNEMO_ADMIN_TOKEN" in result.output


# ── Admin: agent commands ────────────────────────────────────────────────────


class TestAdminAgent:
    @respx.mock
    def test_list(self, runner):
        respx.get(f"{BASE}/v1/admin/agents").mock(
            return_value=Response(200, json={"agents": [
                {"uuid": "ag-1", "address": "bot:jdoe.acme", "display_name": "bot",
                 "status": "active", "operator_username": "jdoe"},
            ]})
        )
        result = runner.invoke(cli, ["admin", "agent", "list"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "bot" in result.output

    @respx.mock
    def test_list_with_filters(self, runner):
        route = respx.get(f"{BASE}/v1/admin/agents").mock(
            return_value=Response(200, json={"agents": []})
        )
        result = runner.invoke(
            cli, ["admin", "agent", "list", "--operator", "op-1", "--status", "departed"],
            env=ADMIN_ENV,
        )
        assert result.exit_code == 0
        assert route.called
        req = route.calls.last.request
        assert "operator=op-1" in str(req.url)
        assert "status=departed" in str(req.url)

    @respx.mock
    def test_depart(self, runner):
        respx.post(f"{BASE}/v1/admin/agents/ag-1/depart").mock(
            return_value=Response(200, json={
                "uuid": "ag-1", "address": "bot:jdoe.acme",
                "capabilities_revoked": 3, "data_expires_at": "2026-04-27",
            })
        )
        result = runner.invoke(cli, ["admin", "agent", "depart", "ag-1"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "Departed" in result.output
        assert "3" in result.output

    @respx.mock
    def test_reinstate(self, runner):
        respx.post(f"{BASE}/v1/admin/agents/ag-1/reinstate").mock(
            return_value=Response(200, json={
                "uuid": "ag-1", "address": "bot:jdoe.acme", "status": "active",
            })
        )
        result = runner.invoke(cli, ["admin", "agent", "reinstate", "ag-1"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "Reinstated" in result.output


    @respx.mock
    def test_rotate_key(self, runner):
        respx.post(f"{BASE}/v1/admin/agents/ag-1/rotate-key").mock(
            return_value=Response(200, json={
                "agent_id": "ag-1", "name": "bot", "address": "bot:jdoe.acme",
                "agent_key": "mnemo_ag_rotated",
                "message": "Save this key",
            })
        )
        result = runner.invoke(cli, ["admin", "agent", "rotate-key", "ag-1"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "mnemo_ag_rotated" in result.output
        assert "previous key is now invalid" in result.output

    @respx.mock
    def test_rotate_key_json(self, runner):
        respx.post(f"{BASE}/v1/admin/agents/ag-1/rotate-key").mock(
            return_value=Response(200, json={
                "agent_id": "ag-1", "name": "bot",
                "agent_key": "mnemo_ag_k",
            })
        )
        result = runner.invoke(cli, ["admin", "--json", "agent", "rotate-key", "ag-1"], env=ADMIN_ENV)
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["agent_key"] == "mnemo_ag_k"


# ── Admin: trust commands ────────────────────────────────────────────────────


class TestAdminTrust:
    @respx.mock
    def test_status(self, runner):
        respx.get(f"{BASE}/v1/admin/trust/status").mock(
            return_value=Response(200, json={"sharing_enabled": True})
        )
        result = runner.invoke(cli, ["admin", "trust", "status"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "ENABLED" in result.output

    @respx.mock
    def test_enable(self, runner):
        respx.post(f"{BASE}/v1/admin/trust/enable").mock(
            return_value=Response(200, json={"sharing_enabled": True})
        )
        result = runner.invoke(cli, ["admin", "trust", "enable"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "ENABLED" in result.output

    @respx.mock
    def test_disable(self, runner):
        respx.post(f"{BASE}/v1/admin/trust/disable").mock(
            return_value=Response(200, json={"sharing_enabled": False, "note": "all off"})
        )
        result = runner.invoke(cli, ["admin", "trust", "disable"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "DISABLED" in result.output

    @respx.mock
    def test_list(self, runner):
        respx.get(f"{BASE}/v1/admin/trust/shares").mock(
            return_value=Response(200, json={"shares": [
                {"capability_id": "cap-1", "grantor_address": "alice:a.org",
                 "grantee_address": "bob:a.org", "view_name": "v1",
                 "created_at": "2026-01-01T00:00:00"},
            ]})
        )
        result = runner.invoke(cli, ["admin", "trust", "list"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "alice" in result.output

    @respx.mock
    def test_revoke(self, runner):
        respx.delete(f"{BASE}/v1/admin/trust/shares/cap-1").mock(
            return_value=Response(200, json={"capability_id": "cap-1", "cascade_count": 2})
        )
        result = runner.invoke(cli, ["admin", "trust", "revoke", "cap-1"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert "Revoked" in result.output
        assert "2" in result.output


# ── Regression tests ─────────────────────────────────────────────────────────


class TestAdminRequestGetNoJson:
    """Regression: _admin_request was passing json=None to GET, which httpx rejects."""

    @respx.mock
    def test_get_without_json_kwarg(self, runner):
        route = respx.get(f"{BASE}/v1/admin/operators").mock(
            return_value=Response(200, json={"operators": []})
        )
        result = runner.invoke(cli, ["admin", "operator", "list"], env=ADMIN_ENV)
        assert result.exit_code == 0
        assert route.called


class TestRegisterOperatorRemoved:
    """register-operator was removed — operator creation is admin-only now."""

    def test_register_operator_not_available(self, runner):
        result = runner.invoke(cli, ["register-operator", "acme"])
        assert result.exit_code != 0
        assert "No such command" in result.output
