"""Plane client initialization for MCP server."""

import os
from http.cookies import SimpleCookie
from types import MethodType
from typing import NamedTuple
from urllib.parse import urlparse

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
    """Context containing Plane client and workspace information."""

    client: PlaneClient
    workspace_slug: str


def _origin_from_client(client: PlaneClient) -> str:
    api_v1_base = client.work_items.config.base_path.rstrip("/")
    if api_v1_base.endswith("/api/v1"):
        return api_v1_base[: -len("/api/v1")]
    parsed = urlparse(api_v1_base)
    return f"{parsed.scheme}://{parsed.netloc}"


def _extract_csrf_from_cookie(cookie_header: str) -> str:
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    for name in ("csrftoken", "csrf_token", "csrf"):
        morsel = cookie.get(name)
        if morsel:
            return morsel.value
    return ""


def ce_session_request(
    client: PlaneClient,
    method: str,
    endpoint: str,
    *,
    params=None,
    data=None,
):
    """Call Plane CE's internal `/api/` endpoints with a browser session.

    Set `PLANE_SESSION_COOKIE` to the complete Cookie header copied from an
    authenticated Plane browser session. For state-changing requests, also set
    `PLANE_CSRF_TOKEN` when the CSRF cookie is not present in that header.
    """
    cookie_header = os.getenv("PLANE_SESSION_COOKIE", "").strip()
    if not cookie_header:
        raise RuntimeError(
            "Plane CE internal API requires PLANE_SESSION_COOKIE. "
            "Set it to the complete Cookie header from an authenticated Plane session."
        )

    origin = _origin_from_client(client)
    url = f"{origin}/api/{endpoint.strip('/')}/"
    # Avoid a duplicated slash if endpoint already ended with one.
    url = url.replace("//api/", "/api/") if origin.endswith("/") else url

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

    response = requests.request(
        method,
        url,
        headers=headers,
        params=params,
        json=data,
        timeout=client.work_items.config.timeout,
    )

    if response.status_code == 204:
        return None
    if 200 <= response.status_code < 300:
        if not response.content:
            return None
        if "application/json" in response.headers.get("content-type", "").lower():
            return response.json()
        return response.text

    try:
        payload = response.json()
    except Exception:
        payload = response.text
    raise HttpError(
        f"HTTP {response.status_code}: {response.reason}",
        response.status_code,
        payload,
    )


def _install_ce_workitem_fallbacks(client: PlaneClient) -> None:
    """Add CE session fallbacks for work-item detail routes.

    Some self-hosted Community Edition versions expose list/create through the
    public `/api/v1/.../work-items/` API, while retrieve/update/delete by UUID
    are available only through the internal session-authenticated
    `/api/.../issues/{id}/` route used by the Plane web app.
    """

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

        query_params = params.model_dump(exclude_none=True) if params else None
        response = ce_session_request(
            client,
            "GET",
            f"workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}",
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

        payload = data.model_dump(exclude_none=True)
        response = ce_session_request(
            client,
            "PATCH",
            f"workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}",
            data=payload,
        )
        # Plane CE's internal PATCH commonly returns 204 No Content. Re-read
        # the record so the MCP tool still returns the updated work item.
        if response is None:
            response = ce_session_request(
                client,
                "GET",
                f"workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}",
            )
        return WorkItem.model_validate(response)

    def delete_with_fallback(
        self,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
    ) -> None:
        try:
            return original_delete(
                workspace_slug=workspace_slug,
                project_id=project_id,
                work_item_id=work_item_id,
            )
        except HttpError as exc:
            if exc.status_code != 404:
                raise

        ce_session_request(
            client,
            "DELETE",
            f"workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}",
        )
        return None

    work_items.retrieve = MethodType(retrieve_with_fallback, work_items)
    work_items.update = MethodType(update_with_fallback, work_items)
    work_items.delete = MethodType(delete_with_fallback, work_items)


def get_plane_client_context() -> PlaneClientContext:
    """Initialize and return a PlaneClient instance with workspace context."""
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

    if access_token:
        client = PlaneClient(base_url=base_url, access_token=access_token)
    else:
        client = PlaneClient(base_url=base_url, api_key=api_key)

    _install_ce_workitem_fallbacks(client)

    return PlaneClientContext(client=client, workspace_slug=workspace_slug)
