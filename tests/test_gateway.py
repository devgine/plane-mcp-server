import io
import json
import logging
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from plane_mcp.__main__ import JSONFormatter, UserContextFilter, build_http_app
from plane_mcp.client import get_plane_client_context
from plane_mcp.gateway import GatewayConfig, load_gateway_config


def test_gateway_is_disabled_when_token_is_absent():
    assert load_gateway_config({}) is None


@pytest.mark.parametrize(
    ("environ", "missing_name"),
    [
        ({"MCP_GATEWAY_TOKEN": "g" * 32, "PLANE_WORKSPACE_SLUG": "personal"}, "PLANE_API_KEY"),
        ({"MCP_GATEWAY_TOKEN": "g" * 32, "PLANE_API_KEY": "plane_api_secret"}, "PLANE_WORKSPACE_SLUG"),
    ],
)
def test_enabled_gateway_rejects_missing_plane_configuration(environ, missing_name):
    with pytest.raises(ValueError, match=missing_name):
        load_gateway_config(environ)


def test_enabled_gateway_rejects_short_capability_without_echoing_it():
    token = "too-short"
    with pytest.raises(ValueError) as caught:
        load_gateway_config(
            {
                "MCP_GATEWAY_TOKEN": token,
                "PLANE_API_KEY": "plane_api_secret",
                "PLANE_WORKSPACE_SLUG": "personal",
            }
        )
    assert "MCP_GATEWAY_TOKEN" in str(caught.value)
    assert token not in str(caught.value)


@pytest.mark.parametrize(
    "token",
    [
        "{aaaaaaaaaaaaaaaaaaaaaaaaa:path}",
        "a" * 16 + "/" + "b" * 16,
        "a" * 16 + "%2F" + "b" * 16,
        "a" * 16 + " " + "b" * 16,
    ],
    ids=["route-template", "slash", "percent-escape", "whitespace"],
)
def test_enabled_gateway_rejects_non_url_safe_capabilities_without_echoing_them(token):
    with pytest.raises(ValueError) as caught:
        load_gateway_config(
            {
                "MCP_GATEWAY_TOKEN": token,
                "PLANE_API_KEY": "plane_api_secret",
                "PLANE_WORKSPACE_SLUG": "personal",
            }
        )
    assert "MCP_GATEWAY_TOKEN" in str(caught.value)
    assert token not in str(caught.value)


def test_enabled_gateway_returns_validated_configuration():
    environ = {
        "MCP_GATEWAY_TOKEN": "g" * 32,
        "PLANE_API_KEY": "plane_api_secret",
        "PLANE_WORKSPACE_SLUG": "personal",
    }
    assert load_gateway_config(environ) == GatewayConfig("g" * 32, "plane_api_secret", "personal")


def _http_server_environment(monkeypatch):
    monkeypatch.setenv("PLANE_OAUTH_PROVIDER_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PLANE_OAUTH_PROVIDER_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PLANE_OAUTH_PROVIDER_BASE_URL", "http://localhost:8211")


def _gateway_environment(monkeypatch):
    _http_server_environment(monkeypatch)
    monkeypatch.setenv("MCP_GATEWAY_TOKEN", "g" * 32)
    monkeypatch.setenv("PLANE_API_KEY", "plane_api_secret")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "personal")
    monkeypatch.delenv("MCP_PATH_PREFIX", raising=False)


def _gateway_log_handler():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(UserContextFilter())
    return logging.getLogger("fastmcp"), handler, stream


def test_gateway_context_uses_server_side_plane_credentials(monkeypatch):
    monkeypatch.setenv("PLANE_API_KEY", "plane_api_secret")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "personal")
    captured = {}

    class CapturingPlaneClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.work_items = SimpleNamespace(
                retrieve=lambda **kwargs: None,
                update=lambda **kwargs: None,
                delete=lambda **kwargs: None,
            )

    monkeypatch.setattr("plane_mcp.client.PlaneClient", CapturingPlaneClient)
    context = get_plane_client_context()

    assert context.workspace_slug == "personal"
    assert captured == {"base_url": "https://api.plane.so", "api_key": "plane_api_secret"}


def test_stdio_logs_keep_configured_workspace(monkeypatch):
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "stdio-workspace")
    record = logging.getLogger("fastmcp.plane_mcp").makeRecord(
        "fastmcp.plane_mcp", logging.INFO, __file__, 0, "stdio event", (), None
    )
    assert UserContextFilter().filter(record)
    assert '"workspace_slug": "stdio-workspace"' in JSONFormatter().format(record)


def test_authenticated_http_logs_keep_token_derived_workspace(monkeypatch):
    monkeypatch.setattr(
        "plane_mcp.__main__.get_access_token",
        lambda: SimpleNamespace(claims={"sub": "user-id", "workspace_slug": "authenticated-workspace"}),
    )
    monkeypatch.setattr("plane_mcp.__main__.get_http_request", lambda: object())
    record = logging.getLogger("fastmcp.plane_mcp").makeRecord(
        "fastmcp.plane_mcp", logging.INFO, __file__, 0, "authenticated event", (), None
    )
    assert UserContextFilter().filter(record)
    rendered = json.loads(JSONFormatter().format(record))
    assert rendered["user_id"] == "user-id"
    assert rendered["workspace_slug"] == "authenticated-workspace"


def test_gateway_route_is_absent_when_disabled(monkeypatch):
    _http_server_environment(monkeypatch)
    monkeypatch.delenv("MCP_GATEWAY_TOKEN", raising=False)
    with TestClient(build_http_app()) as client:
        response = client.post(
            "/http/chatgpt/" + "g" * 32 + "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        )
    assert response.status_code == 404


def test_wrong_gateway_capability_returns_not_found(monkeypatch):
    _gateway_environment(monkeypatch)
    with TestClient(build_http_app()) as client:
        response = client.post(
            "/http/chatgpt/" + "x" * 32 + "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        )
    assert response.status_code == 404
    assert "g" * 32 not in response.text
    assert "plane_api_secret" not in response.text
    assert "personal" not in response.text


def test_correct_gateway_capability_initializes_without_auth_headers(monkeypatch):
    _gateway_environment(monkeypatch)
    with TestClient(build_http_app()) as client:
        response = client.post(
            "/http/chatgpt/" + "g" * 32 + "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
                "id": 1,
            },
        )
    assert response.status_code == 200
    assert "plane_api_secret" not in response.text
    assert "g" * 32 not in response.text
    assert "personal" not in response.text


def test_gateway_tool_call_uses_server_credentials_despite_hostile_auth_headers(monkeypatch):
    _gateway_environment(monkeypatch)
    captured = {}

    class CapturingPlaneClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.work_items = SimpleNamespace(
                retrieve=lambda **kwargs: None,
                update=lambda **kwargs: None,
                delete=lambda **kwargs: None,
            )
            self.projects = SimpleNamespace(delete=lambda **kwargs: captured.update(project=kwargs))

    monkeypatch.setattr("plane_mcp.client.PlaneClient", CapturingPlaneClient)
    with TestClient(build_http_app()) as client:
        response = client.post(
            "/http/chatgpt/" + "g" * 32 + "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer caller-controlled-secret",
                "X-Workspace-Slug": "caller-controlled-workspace",
            },
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "project",
                    "arguments": {"action": "delete", "project_id": "project-id"},
                },
                "id": 1,
            },
        )
    assert response.status_code == 200
    assert '"isError":false' in response.text
    assert captured == {
        "client": {"base_url": "https://api.plane.so", "api_key": "plane_api_secret"},
        "project": {"workspace_slug": "personal", "project_id": "project-id"},
    }


def test_gateway_request_logs_do_not_disclose_capability_pat_or_workspace(monkeypatch):
    _gateway_environment(monkeypatch)
    fastmcp_logger, handler, stream = _gateway_log_handler()
    with TestClient(build_http_app()) as client:
        fastmcp_logger.addHandler(handler)
        try:
            response = client.post(
                "/http/chatgpt/" + "g" * 32 + "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": "Bearer plane_pat_secret",
                },
                json={
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                    "id": 1,
                },
            )
        finally:
            fastmcp_logger.removeHandler(handler)
    assert response.status_code == 200
    logs = stream.getvalue()
    assert "g" * 32 not in logs
    assert "plane_pat_secret" not in logs
    assert "personal" not in logs


def test_gateway_startup_json_does_not_emit_secrets_or_workspace(monkeypatch):
    _gateway_environment(monkeypatch)
    fastmcp_logger, handler, stream = _gateway_log_handler()
    fastmcp_logger.addHandler(handler)
    try:
        build_http_app()
    finally:
        fastmcp_logger.removeHandler(handler)
    entries = [json.loads(line) for line in stream.getvalue().splitlines()]
    gateway_entry = next(entry for entry in entries if entry.get("message") == "ChatGPT no-auth gateway enabled")
    rendered = json.dumps(gateway_entry)
    assert "plane_api_secret" not in rendered
    assert "g" * 32 not in rendered
    assert "personal" not in rendered


def test_gateway_trailing_slash_does_not_redirect_or_disclose_capability(monkeypatch):
    _gateway_environment(monkeypatch)
    token = "g" * 32
    with TestClient(build_http_app()) as client:
        response = client.post(
            f"/http/chatgpt/{token}/mcp/",
            follow_redirects=False,
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        )
    assert response.status_code == 404
    for value in [response.text, *response.headers.values()]:
        assert token not in value


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/http/mcp"), ("POST", "/http/api-key/mcp"), ("GET", "/sse")],
)
def test_gateway_enabled_app_preserves_existing_auth_route_boundaries(monkeypatch, method, path):
    _gateway_environment(monkeypatch)
    with TestClient(build_http_app()) as client:
        response = client.request(method, path, follow_redirects=False, json={} if method == "POST" else None)
    assert response.status_code == 401


def test_gateway_route_respects_path_prefix(monkeypatch):
    _gateway_environment(monkeypatch)
    monkeypatch.setenv("MCP_PATH_PREFIX", "/plane")
    with TestClient(build_http_app()) as client:
        response = client.post(
            "/plane/http/chatgpt/" + "g" * 32 + "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
                "id": 1,
            },
        )
    assert response.status_code == 200
