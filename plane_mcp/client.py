"""Plane client initialization for MCP server."""

import os
from types import MethodType
from typing import NamedTuple

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


def _install_ce_workitem_fallbacks(client: PlaneClient) -> None:
    """Add legacy `/issues/` fallbacks for Plane CE work-item detail routes.

    Some self-hosted Community Edition versions expose list/create through
    `/work-items/` but still use `/issues/{id}/` for retrieve/update/delete.
    The official SDK targets only `/work-items/{id}/`, which returns 404 on
    those CE releases. Retry only on 404 so genuine auth/server errors are
    preserved.
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
        response = self._get(
            f"{workspace_slug}/projects/{project_id}/issues/{work_item_id}",
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

        response = self._patch(
            f"{workspace_slug}/projects/{project_id}/issues/{work_item_id}",
            data.model_dump(exclude_none=True),
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

        self._delete(f"{workspace_slug}/projects/{project_id}/issues/{work_item_id}")
        return None

    work_items.retrieve = MethodType(retrieve_with_fallback, work_items)
    work_items.update = MethodType(update_with_fallback, work_items)
    work_items.delete = MethodType(delete_with_fallback, work_items)


def get_plane_client_context() -> PlaneClientContext:
    """
    Initialize and return a PlaneClient instance with workspace context.

    Authentication is handled by the PlaneOAuthProvider, which supports:
    1. Environment variables (PLANE_API_KEY + PLANE_WORKSPACE_SLUG)
    2. HTTP headers (x-api-key + x-workspace-slug)
    3. OAuth access token

    Environment variables:
    - PLANE_INTERNAL_BASE_URL: Internal URL for Plane API (preferred for server-to-server calls)
    - PLANE_BASE_URL: Base URL for Plane API (fallback, default: https://api.plane.so)

    Returns:
        PlaneClientContext containing configured PlaneClient instance and workspace slug

    Raises:
        ConfigurationError: If access token is not available or workspace slug is missing
    """
    base_url = os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "https://api.plane.so")
    workspace_slug = os.getenv("PLANE_WORKSPACE_SLUG", "")

    api_key = os.getenv("PLANE_API_KEY", "")
    access_token = None

    # Get access token from the OAuth provider (which handles all auth methods)
    stored_access_token: AccessToken | None = get_access_token()
    if stored_access_token:
        # Determine authentication method to use appropriate PlaneClient constructor
        auth_method = stored_access_token.claims.get("auth_method", "oauth")
        token = stored_access_token.token
        workspace_slug = stored_access_token.claims.get("workspace_slug", "")

        # For API key auth methods, use api_key parameter; for OAuth, use access_token
        if auth_method in ("api_key_env", "api_key_header"):
            api_key = token
        else:
            access_token = token

    if access_token:
        client = PlaneClient(
            base_url=base_url,
            access_token=access_token,
        )
    else:
        client = PlaneClient(
            base_url=base_url,
            api_key=api_key,
        )

    _install_ce_workitem_fallbacks(client)

    return PlaneClientContext(
        client=client,
        workspace_slug=workspace_slug,
    )
