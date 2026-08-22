# ChatGPT No-Auth Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in capability-URL MCP gateway that lets a ChatGPT Web No Auth connector use Plane credentials held only in server-side secrets.

**Architecture:** A small configuration module validates the three gateway secrets and returns an immutable configuration object or `None`. The HTTP app builder conditionally mounts a no-auth FastMCP instance beneath an exact high-entropy path; the existing client fallback supplies the server-owned Plane PAT and workspace without placing either in an inbound request.

**Tech Stack:** Python 3.10+, FastMCP, Starlette, pytest, Ruff, uv/build.

**Spec:** `docs/superpowers/specs/2026-08-21-chatgpt-no-auth-gateway-design.md`

## Global Constraints

- Existing OAuth, header-PAT, SSE, and stdio behavior must remain unchanged.
- The gateway is disabled unless `MCP_GATEWAY_TOKEN` is set.
- An enabled gateway requires a token of at least 32 characters, `PLANE_API_KEY`, and `PLANE_WORKSPACE_SLUG`.
- The Plane PAT and workspace slug must not appear in connector URLs, MCP responses, or application logs.
- The gateway token must not appear in application logs or error messages.
- The gateway must be deployed only over TLS, with capability paths omitted or redacted from reverse-proxy access logs.
- No new runtime dependency is permitted.

---

### Task 1: Validate opt-in gateway configuration

**Files:**
- Create: `plane_mcp/gateway.py`
- Create: `tests/test_gateway.py`

**Interfaces:**
- Consumes: environment mapping values named `MCP_GATEWAY_TOKEN`, `PLANE_API_KEY`, and `PLANE_WORKSPACE_SLUG`.
- Produces: `GatewayConfig(NamedTuple)` with `token: str`, `plane_api_key: str`, and `workspace_slug: str`; `load_gateway_config(environ: Mapping[str, str] | None = None) -> GatewayConfig | None`.

- [ ] **Step 1: Write failing configuration tests**

```python
import pytest

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


def test_enabled_gateway_returns_validated_configuration():
    environ = {
        "MCP_GATEWAY_TOKEN": "g" * 32,
        "PLANE_API_KEY": "plane_api_secret",
        "PLANE_WORKSPACE_SLUG": "personal",
    }
    assert load_gateway_config(environ) == GatewayConfig("g" * 32, "plane_api_secret", "personal")
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `pytest tests/test_gateway.py -v`

Expected: collection fails because `plane_mcp.gateway` does not exist.

- [ ] **Step 3: Implement the minimal validated configuration loader**

```python
import os
from collections.abc import Mapping
from typing import NamedTuple


class GatewayConfig(NamedTuple):
    token: str
    plane_api_key: str
    workspace_slug: str


def load_gateway_config(environ: Mapping[str, str] | None = None) -> GatewayConfig | None:
    values = os.environ if environ is None else environ
    token = values.get("MCP_GATEWAY_TOKEN", "")
    if not token:
        return None
    if len(token) < 32:
        raise ValueError("MCP_GATEWAY_TOKEN must contain at least 32 characters")
    api_key = values.get("PLANE_API_KEY", "")
    if not api_key:
        raise ValueError("PLANE_API_KEY is required when MCP_GATEWAY_TOKEN is set")
    workspace_slug = values.get("PLANE_WORKSPACE_SLUG", "")
    if not workspace_slug:
        raise ValueError("PLANE_WORKSPACE_SLUG is required when MCP_GATEWAY_TOKEN is set")
    return GatewayConfig(token, api_key, workspace_slug)
```

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run: `pytest tests/test_gateway.py -v`

Expected: 5 tests pass.

- [ ] **Step 5: Commit the configuration boundary**

```bash
git add -- plane_mcp/gateway.py tests/test_gateway.py
git commit -m "feat: validate no-auth gateway configuration"
```

### Task 2: Build and conditionally mount the capability gateway

**Files:**
- Modify: `plane_mcp/server.py`
- Modify: `plane_mcp/__main__.py`
- Modify: `tests/test_gateway.py`

**Interfaces:**
- Consumes: `load_gateway_config() -> GatewayConfig | None` from Task 1 and existing `_configured(mcp: FastMCP) -> FastMCP`.
- Produces: `get_gateway_mcp() -> FastMCP`; `build_http_app() -> Starlette`; exact mount `/http/chatgpt/{token}/mcp`, respecting `MCP_PATH_PREFIX`.

- [ ] **Step 1: Add failing routing tests using the real ASGI application**

Append tests that set environment variables with `monkeypatch`, call `build_http_app()`, and use `TestClient`:

```python
from starlette.testclient import TestClient

from plane_mcp.__main__ import build_http_app


def _gateway_environment(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_TOKEN", "g" * 32)
    monkeypatch.setenv("PLANE_API_KEY", "plane_api_secret")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "personal")
    monkeypatch.delenv("MCP_PATH_PREFIX", raising=False)


def test_gateway_route_is_absent_when_disabled(monkeypatch):
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
```

- [ ] **Step 2: Run the routing tests and confirm RED**

Run: `pytest tests/test_gateway.py -v`

Expected: import fails because `build_http_app` is not defined.

- [ ] **Step 3: Add the no-auth FastMCP factory**

In `plane_mcp/server.py`, add:

```python
def get_gateway_mcp() -> FastMCP:
    return _configured(
        FastMCP(
            "Plane MCP Server (ChatGPT gateway)",
            instructions=SERVER_INSTRUCTIONS,
        )
    )
```

- [ ] **Step 4: Extract HTTP construction and conditionally mount the gateway**

In `plane_mcp/__main__.py`, import `load_gateway_config` and
`get_gateway_mcp`. Extract all HTTP app construction from `main()` into
`build_http_app() -> Starlette`. Build the three existing apps exactly as now.
When configuration is enabled, additionally create
`gateway_app = get_gateway_mcp().http_app(stateless_http=True)` and append:

```python
Mount(prefix + f"/http/chatgpt/{gateway_config.token}", app=gateway_app)
```

Change `combined_lifespan` to accept the fixed existing apps plus an optional
gateway app and enter its lifespan only when present. `main()` calls
`build_http_app()` and passes the result to `uvicorn.run`.

- [ ] **Step 5: Run the focused routing tests and confirm GREEN**

Run: `pytest tests/test_gateway.py -v`

Expected: all configuration and routing tests pass.

- [ ] **Step 6: Run existing HTTP/auth regression tests**

Run: `pytest tests/test_stateless_http.py tests/test_oauth_security.py -v`

Expected: all tests pass with unchanged OAuth and header behavior.

- [ ] **Step 7: Commit the gateway transport**

```bash
git add -- plane_mcp/server.py plane_mcp/__main__.py tests/test_gateway.py
git commit -m "feat: add capability URL gateway for ChatGPT"
```

### Task 3: Prove server-side Plane credential resolution and secret-safe logging

**Files:**
- Modify: `tests/test_gateway.py`
- Modify: `plane_mcp/__main__.py` only if a failing leakage test requires it.

**Interfaces:**
- Consumes: existing `get_plane_client_context() -> PlaneClientContext`, `build_http_app() -> Starlette`, and JSON logging configuration.
- Produces: regression evidence that no request credential is required and no configured secret is logged or returned.

- [ ] **Step 1: Add a failing-or-characterizing client credential test**

Patch `plane_mcp.client.PlaneClient` at the external SDK boundary and assert on
the real returned context, not on mock call counts:

```python
from plane_mcp.client import get_plane_client_context


def test_gateway_context_uses_server_side_plane_credentials(monkeypatch):
    monkeypatch.setenv("PLANE_API_KEY", "plane_api_secret")
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "personal")
    captured = {}

    class CapturingPlaneClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("plane_mcp.client.PlaneClient", CapturingPlaneClient)
    context = get_plane_client_context()

    assert context.workspace_slug == "personal"
    assert captured == {"base_url": "https://api.plane.so", "api_key": "plane_api_secret"}
```

- [ ] **Step 2: Run the client test and record whether it is RED or a characterization pass**

Run: `pytest tests/test_gateway.py::test_gateway_context_uses_server_side_plane_credentials -v`

Expected: PASS is acceptable because this verifies an existing behavior intentionally reused by the design. If request context behavior interferes, make the test execute inside the gateway MCP call and keep the same observable assertions.

- [ ] **Step 3: Add a log-capture leakage test**

```python
def test_gateway_startup_logging_does_not_emit_secrets(monkeypatch, caplog):
    _gateway_environment(monkeypatch)
    build_http_app()
    rendered = caplog.text
    assert "plane_api_secret" not in rendered
    assert "g" * 32 not in rendered
```

This catches future logging of either full gateway route or Plane PAT. If the
current extraction emits no startup record, add one constant message such as
`"ChatGPT no-auth gateway enabled"`; never interpolate configuration.

- [ ] **Step 4: Run all gateway tests and confirm GREEN**

Run: `pytest tests/test_gateway.py -v`

Expected: all tests pass and captured output contains neither secret.

- [ ] **Step 5: Commit the credential and leakage regression coverage**

```bash
git add -- tests/test_gateway.py plane_mcp/__main__.py
git commit -m "test: protect gateway credentials from disclosure"
```

### Task 4: Document ChatGPT Web and Traefik deployment

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: exact environment variables and route delivered by Tasks 1–2.
- Produces: operator instructions for secret generation, container configuration, ChatGPT setup, Traefik forwarding, redaction, and rotation.

- [ ] **Step 1: Add the operator documentation**

Add a section after the hosted PAT transport containing these concrete items:

```markdown
### ChatGPT Web with No Auth — private single-workspace gateway

Generate a separate capability (do not reuse the Plane PAT):

    openssl rand -hex 32

Supply `MCP_GATEWAY_TOKEN`, `PLANE_API_KEY`, and `PLANE_WORKSPACE_SLUG` from
container/orchestrator secrets. Configure the connector as **No Auth** with:

    https://mcp.example.com/http/chatgpt/<MCP_GATEWAY_TOKEN>/mcp

The Plane PAT remains server-side. The URL itself is a credential: require
HTTPS, do not share it, redact `/http/chatgpt/*` in proxy access logs, use a
least-privilege Plane token, and rotate `MCP_GATEWAY_TOKEN` if the URL leaks.
Prefer OAuth for per-user identity and revocation.
```

Include a minimal Traefik label/file-provider example that forwards the host to
port 8211 and does not inject `Authorization` or `X-Workspace-slug`. Explain
that access logging must be disabled for this router or redacted by the
deployment's logging pipeline because Traefik configuration differs by version.

Extend the authentication environment table with all three values and state
that setting `MCP_GATEWAY_TOKEN` activates startup validation.

- [ ] **Step 2: Review documentation for accidental real secrets**

Run: `rg -n 'plane_api_[A-Za-z0-9_-]{10,}|MCP_GATEWAY_TOKEN=.+' README.md`

Expected: no real-looking assigned secrets; only placeholders and variable names.

- [ ] **Step 3: Run documentation and formatting checks**

Run: `pytest tests/test_docs.py -v && ruff format --check plane_mcp tests && ruff check plane_mcp tests`

Expected: all checks pass.

- [ ] **Step 4: Commit deployment documentation**

```bash
git add -- README.md
git commit -m "docs: configure ChatGPT no-auth gateway"
```

### Task 5: Full verification and release-ready diff

**Files:**
- Verify only; modify earlier files only to correct failures.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: a clean, tested branch ready for user-approved push.

- [ ] **Step 1: Run the entire offline test suite**

Run: `pytest`

Expected: all tests pass; credentialed live integration tests may skip according to their existing markers.

- [ ] **Step 2: Run final lint and formatting checks**

Run: `ruff format --check plane_mcp tests && ruff check plane_mcp tests`

Expected: both commands exit 0.

- [ ] **Step 3: Build the distributable package**

Run: `uv build`

Expected: source and wheel artifacts build successfully.

- [ ] **Step 4: Inspect branch hygiene and secret leakage**

Run:

```bash
git status --short
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

Expected: no uncommitted files, no whitespace errors, and only the design,
plan, gateway implementation, tests, and documentation appear in the branch.
Inspect the complete diff and confirm that no concrete PAT or generated
capability is present.

- [ ] **Step 5: Report the exact verification evidence**

Report test counts, skipped live tests, lint/format/build exit status, commit
list, and any environment limitation. Do not push until the user explicitly
authorizes pushing the reviewed branch.
