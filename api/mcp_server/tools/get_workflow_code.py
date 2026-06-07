"""MCP tool that returns a workflow as SDK TypeScript code.

Companion to `save_workflow`: the LLM calls `get_workflow_code` to see
the current state of a workflow as editable code, mutates it, and calls
`save_workflow` with the new code. Storage stays JSON; the TS form is
an ephemeral projection for the LLM edit loop.

This tool is a THIN MCP wrapper around
`api.services.workflow.authoring.get_workflow_sdk_code`, which selects the
working copy (latest draft → latest published → legacy
`workflow.workflow_definition`) — matching the UI's "whichever is the
working copy" behavior so the LLM sees what a human editor would see.
"""

from __future__ import annotations

from typing import Any

from api.mcp_server.auth import authenticate_mcp_request
from api.mcp_server.tracing import traced_tool
from api.services.workflow.authoring import get_workflow_sdk_code


@traced_tool
async def get_workflow_code(workflow_id: int) -> dict[str, Any]:
    """Return the workflow as SDK TypeScript code the LLM can edit.

    Output shape:
        {"code": "<TS source>", "workflow_id": int, "version": "draft" | "published" | "legacy"}

    The LLM edits `code`, then calls `save_workflow(workflow_id, code)`.
    """
    user = await authenticate_mcp_request()
    result = await get_workflow_sdk_code(workflow_id, user)
    # Preserve this tool's historical output shape (the service additionally
    # returns `status`/`version_number`, which the agent-facing `get_workflow`
    # tool surfaces; this terser projection is the documented contract here).
    return {
        "workflow_id": result["workflow_id"],
        "name": result["name"],
        "version": result["version"],
        "code": result["code"],
    }
