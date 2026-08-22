/Users/yosribahri/.zlogin:9: nice(5) failed: operation not permitted
import os
import re
from collections.abc import Mapping
from typing import NamedTuple

_CAPABILITY_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,}")


class GatewayConfig(NamedTuple):
    token: str
    plane_api_key: str
    workspace_slug: str


def load_gateway_config(environ: Mapping[str, str] | None = None) -> GatewayConfig | None:
    values = os.environ if environ is None else environ
    token = values.get("MCP_GATEWAY_TOKEN", "")
    if not token:
        return None
    if _CAPABILITY_PATTERN.fullmatch(token) is None:
        raise ValueError("MCP_GATEWAY_TOKEN must contain at least 32 URL-safe characters")
    api_key = values.get("PLANE_API_KEY", "")
    if not api_key:
        raise ValueError("PLANE_API_KEY is required when MCP_GATEWAY_TOKEN is set")
    workspace_slug = values.get("PLANE_WORKSPACE_SLUG", "")
    if not workspace_slug:
        raise ValueError("PLANE_WORKSPACE_SLUG is required when MCP_GATEWAY_TOKEN is set")
    return GatewayConfig(token, api_key, workspace_slug)
