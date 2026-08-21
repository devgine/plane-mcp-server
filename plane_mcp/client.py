"""Plane client initialization for MCP server."""

import json
import os
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from types import MethodType
from typing import NamedTuple
from urllib.parse import urlparse
from uuid import UUID

import redis
import requests
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.logging import get_logger
from plane import PlaneClient
from plane.errors.errors import HttpError
from plane.models.query_params import RetrieveQueryParams
from plane.models.work_items import UpdateWorkItem, WorkItem, WorkItemDetail

logger = get_logger(__name__)


class PlaneClientContext(NamedTuple):
    client: PlaneClient
    workspace_slug: str


def _public_origin() -> str:
    configured = os.getenv("PLANE_SESSION_BASE_URL", "").strip() or os.getenv("PLANE_BASE_URL", "").strip()
    if not configured:
        raise RuntimeError("Plane CE session API requires PLANE_BASE_URL or PLANE_SESSION_BASE_URL.")
    parsed = urlparse(configured)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError("PLANE_SESSION_BASE_URL/PLANE_BASE_URL must be an absolute URL.")
    return f"{parsed.scheme}://{parsed.netloc}"


def _extract_csrf_from_cookie(cookie_header: str) -> str:
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    for name in ("csrftoken", "csrf_token", "csrf"):
        morsel = cookie.get(name)
        if morsel:
            return morsel.value
    return ""


def _page_metadata_target(endpoint: str, data) -> tuple[str, str, str] | None:
    if not isinstance(data, dict) or "name" not in data:
        return None
    parts = endpoint.strip("/").split("/")
    if len(parts) != 6 or parts[0] != "workspaces" or parts[2] != "projects" or parts[4] != "pages":
        return None
    return parts[1], parts[3], parts[5]


def _force_close_plane_live_document(page_id: str) -> None:
    """Ask Plane Live/Hocuspocus to close active copies of a page before external mutation."""
    redis_url = os.getenv("PLANE_LIVE_REDIS_URL", "").strip() or os.getenv("PLANE_REDIS_URL", "").strip()
    if not redis_url:
        raise RuntimeError(
            "Plane page title synchronization requires PLANE_LIVE_REDIS_URL (or PLANE_REDIS_URL) "
            "pointing to the Redis instance used by Plane Live."
        )

    command = {
        "command": "force_close",
        "docId": page_id,
        "reason": "admin_request",
        "code": 4000,
        "originServer": "plane-mcp",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    client = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        receivers = client.publish("hocuspocus:admin", json.dumps(command))
    finally:
        client.close()

    logger.info("Plane Live force_close published for page %s to %s subscriber(s)", page_id, receivers)
    if receivers < 1:
        raise RuntimeError(
            "Plane Live Redis command had no subscribers. Verify PLANE_LIVE_REDIS_URL points to the Redis used by Plane Live."
        )

    # Plane's force-close flow waits ~800 ms before unloading locally. Give the
    # Live process enough time to flush any old document state before we write
    # the authoritative metadata and Yjs values below.
    time.sleep(1.2)


def _sync_page_title_after_metadata_patch(client: PlaneClient, endpoint: str, data) -> None:
    """Keep Plane page metadata and its Live/Yjs title in sync."""
    target = _page_metadata_target(endpoint, data)
    if target is None:
        return
    workspace_slug, project_id, page_id = target
    title = str(data.get("name") or "")

    from plane_mcp.page_yjs import sync_project_page_title_yjs

    logger.info("Synchronizing Plane page Yjs title for page %s", page_id)
    sync_project_page_title_yjs(client, workspace_slug, project_id, page_id, title)

    # A Live document that was active before this MCP operation may have flushed
    # its previous metadata while closing. Re-assert the title in the Page row
    # after writing the new Yjs state, without triggering this synchronization
    # hook recursively.
    ce_session_request(
        client,
        "PATCH",
        endpoint,
        data={"name": title},
        sync_page_title=False,
    )
    logger.info("Plane page Yjs title synchronized for page %s", page_id)


def ce_session_request(
    client: PlaneClient,
    method: str,
    endpoint: str,
    *,
    params=None,
    data=None,
    response_binary: bool = False,
    sync_page_title: bool = True,
):
    """Call Plane CE's internal `/api/` endpoints with a browser session."""
    cookie_header = os.getenv("PLANE_SESSION_COOKIE", "").strip()
    if not cookie_header:
        raise RuntimeError(
            "Plane CE internal API requires PLANE_SESSION_COOKIE. "
            "Set it to the complete Cookie header from an authenticated Plane session."
        )

    origin = _public_origin()
    url = f"{origin}/api/{endpoint.strip('/')}/"

    if response_binary:
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/octet-stream",
            "Cookie": cookie_header,
            "Origin": origin,
            "Referer": f"{origin}/",
        }
    else:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cookie": cookie_header,
            "Origin": origin,
            "Referer": f"{origin}/",
        }

    if method.upper() not in ("GET", "HEAD", "OPTIONS"):
        csrf = os.getenv("PLANE_CSRF_TOKEN", "").strip() or _extract_csrf_from_cookie(cookie_header)
        if csrf:
            headers["X-CSRFToken"] = csrf

    page_target = _page_metadata_target(endpoint, data) if sync_page_title and method.upper() == "PATCH" else None
    if page_target is not None:
        _force_close_plane_live_document(page_target[2])

    logger.info("Plane CE session fallback %s %s", method.upper(), url)
    response = requests.request(
        method,
        url,
        headers=headers,
        params=params,
        json=data,
        timeout=client.work_items.config.timeout,
    )
    logger.info("Plane CE session fallback response: HTTP %s", response.status_code)

    if response.status_code == 204:
        if page_target is not None:
            _sync_page_title_after_metadata_patch(client, endpoint, data)
        return None
    if 200 <= response.status_code < 300:
        if page_target is not None:
            _sync_page_title_after_metadata_patch(client, endpoint, data)
        if response_binary:
            return response.content
        if not response.content:
            return None
        if "application/json" in response.headers.get("content-type", "").lower():
            return response.json()
        return response.text

    try:
        payload = response.json()
    except Exception:
        payload = response.text
    raise HttpError(f"HTTP {response.status_code}: {response.reason}", response.status_code, payload)


def _resolve_workitem_uuid(client: PlaneClient, workspace_slug: str, work_item_id: str) -> tuple[str, WorkItemDetail | None]:
    try:
        UUID(work_item_id)
        return work_item_id, None
    except ValueError:
        pass

    project_identifier, separator, sequence = work_item_id.rpartition("-")
    if not separator or not project_identifier or not sequence.isdigit():
        return work_item_id, None

    detail = client.work_items.retrieve_by_identifier(
        workspace_slug=workspace_slug,
        project_identifier=project_identifier,
        issue_identifier=int(sequence),
    )
    resolved = str(detail.id)
    logger.info("Resolved Plane work item identifier %s to UUID %s", work_item_id, resolved)
    return resolved, detail


def _install_ce_workitem_fallbacks(client: PlaneClient) -> None:
    work_items = client.work_items
    original_retrieve = work_items.retrieve
    original_update = work_items.update
    original_delete = work_items.delete

    def retrieve_with_fallback(
        self,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        params: RetrieveQueryParams | None = None,
    ) -> WorkItemDetail:
        try:
            return original_retrieve(
                workspace_slug=workspace_slug,
                project_id=project_id,
                work_item_id=work_item_id,
                params=params,
            )
        except HttpError as exc:
            if exc.status_code != 404:
                raise
        resolved_id, resolved_detail = _resolve_workitem_uuid(client, workspace_slug, work_item_id)
        if resolved_detail is not None and params is None:
            return resolved_detail
        query_params = params.model_dump(exclude_none=True) if params else None
        response = ce_session_request(
            client,
            "GET",
            f"workspaces/{workspace_slug}/projects/{project_id}/issues/{resolved_id}",
            params=query_params,
        )
        return WorkItemDetail.model_validate(response)

    def update_with_fallback(
        self,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        data: UpdateWorkItem,
    ) -> WorkItem:
        try:
            return original_update(
                workspace_slug=workspace_slug,
                project_id=project_id,
                work_item_id=work_item_id,
                data=data,
            )
        except HttpError as exc:
            if exc.status_code != 404:
                raise
        resolved_id, _ = _resolve_workitem_uuid(client, workspace_slug, work_item_id)
        response = ce_session_request(
            client,
            "PATCH",
            f"workspaces/{workspace_slug}/projects/{project_id}/issues/{resolved_id}",
            data=data.model_dump(exclude_none=True),
        )
        if response is None:
            response = ce_session_request(
                client,
                "GET",
                f"workspaces/{workspace_slug}/projects/{project_id}/issues/{resolved_id}",
            )
        return WorkItem.model_validate(response)

    def delete_with_fallback(self, workspace_slug: str, project_id: str, work_item_id: str) -> None:
        try:
            return original_delete(
                workspace_slug=workspace_slug,
                project_id=project_id,
                work_item_id=work_item_id,
            )
        except HttpError as exc:
            if exc.status_code != 404:
                raise
        resolved_id, _ = _resolve_workitem_uuid(client, workspace_slug, work_item_id)
        ce_session_request(
            client,
            "DELETE",
            f"workspaces/{workspace_slug}/projects/{project_id}/issues/{resolved_id}",
        )
        return None

    work_items.retrieve = MethodType(retrieve_with_fallback, work_items)
    work_items.update = MethodType(update_with_fallback, work_items)
    work_items.delete = MethodType(delete_with_fallback, work_items)


def get_plane_client_context() -> PlaneClientContext:
    base_url = os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "https://api.plane.so")
    workspace_slug = os.getenv("PLANE_WORKSPACE_SLUG", "")
    api_key = os.getenv("PLANE_API_KEY", "")
    access_token = None

    stored_access_token: AccessToken | None = get_access_token()
    if stored_access_token:
        auth_method = stored_access_token.claims.get("auth_method", "oauth")
        token = stored_access_token.token
        workspace_slug = stored_access_token.claims.get("workspace_slug", "")
        if auth_method in ("api_key_env", "api_key_header"):
            api_key = token
        else:
            access_token = token

    client = (
        PlaneClient(base_url=base_url, access_token=access_token)
        if access_token
        else PlaneClient(base_url=base_url, api_key=api_key)
    )
    _install_ce_workitem_fallbacks(client)
    return PlaneClientContext(client=client, workspace_slug=workspace_slug)
