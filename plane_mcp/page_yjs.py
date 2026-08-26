"""Helpers for keeping Plane CE page metadata and Yjs documents in sync."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from plane import PlaneClient

from plane_mcp.client import ce_session_request

_TITLE_HELPER = Path("/app/yjs-helper/update-title.mjs")
_BODY_HELPER = Path("/app/yjs-helper/update-body.mjs")


def _run_yjs_helper(helper: Path, payload: dict[str, str]) -> str:
    process = subprocess.run(
        ["node", str(helper)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(f"Yjs helper {helper.name} failed: {process.stderr.strip()}")

    updated_binary = process.stdout.strip()
    if not updated_binary:
        raise RuntimeError(f"Yjs helper {helper.name} returned an empty document")
    return updated_binary


def _fetch_project_page_binary(
    client: PlaneClient,
    workspace_slug: str,
    project_id: str,
    page_id: str,
) -> tuple[str, bytes]:
    endpoint = f"workspaces/{workspace_slug}/projects/{project_id}/pages/{page_id}/description"
    binary = ce_session_request(client, "GET", endpoint, response_binary=True)
    if not isinstance(binary, (bytes, bytearray)) or not binary:
        raise RuntimeError("Plane page description endpoint did not return a Yjs binary document")
    return endpoint, bytes(binary)


def sync_project_page_title_yjs(
    client: PlaneClient,
    workspace_slug: str,
    project_id: str,
    page_id: str,
    title: str,
) -> None:
    """Rewrite only the Yjs `title` fragment while preserving the page body."""
    endpoint, binary = _fetch_project_page_binary(client, workspace_slug, project_id, page_id)
    updated_binary = _run_yjs_helper(
        _TITLE_HELPER,
        {
            "description_binary": base64.b64encode(binary).decode("ascii"),
            "title": title,
        },
    )

    ce_session_request(
        client,
        "PATCH",
        endpoint,
        data={"description_binary": updated_binary},
    )


def sync_project_page_body_yjs(
    client: PlaneClient,
    workspace_slug: str,
    project_id: str,
    page_id: str,
    description_html: str,
) -> None:
    """Replace the Yjs `default` fragment from HTML while preserving the title."""
    endpoint, binary = _fetch_project_page_binary(client, workspace_slug, project_id, page_id)
    updated_binary = _run_yjs_helper(
        _BODY_HELPER,
        {
            "description_binary": base64.b64encode(binary).decode("ascii"),
            "description_html": description_html,
        },
    )

    # Keep the database HTML and the authoritative Yjs document aligned in the
    # same write. Plane Live will regenerate description_json on its next save.
    ce_session_request(
        client,
        "PATCH",
        endpoint,
        data={
            "description_binary": updated_binary,
            "description_html": description_html,
        },
    )
