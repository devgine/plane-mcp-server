"""Helpers for keeping Plane CE page metadata and Yjs document titles in sync."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

from plane import PlaneClient

from plane_mcp.client import ce_session_request

_HELPER = Path("/app/yjs-helper/update-title.mjs")


def sync_project_page_title_yjs(
    client: PlaneClient,
    workspace_slug: str,
    project_id: str,
    page_id: str,
    title: str,
) -> None:
    """Rewrite only the Yjs `title` fragment while preserving the page body."""
    endpoint = f"workspaces/{workspace_slug}/projects/{project_id}/pages/{page_id}/description"
    binary = ce_session_request(client, "GET", endpoint, response_binary=True)
    if not isinstance(binary, (bytes, bytearray)) or not binary:
        raise RuntimeError("Plane page description endpoint did not return a Yjs binary document")

    process = subprocess.run(
        ["node", str(_HELPER)],
        input=json.dumps(
            {
                "description_binary": base64.b64encode(binary).decode("ascii"),
                "title": title,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(f"Yjs title helper failed: {process.stderr.strip()}")

    updated_binary = process.stdout.strip()
    if not updated_binary:
        raise RuntimeError("Yjs title helper returned an empty document")

    ce_session_request(
        client,
        "PATCH",
        endpoint,
        data={"description_binary": updated_binary},
    )
