"""MCP tool that accepts LLM-authored SDK TypeScript and creates a new workflow.

Companion to `save_workflow`: where `save_workflow` updates an existing
workflow as a new draft, `create_workflow` brings a workflow into being
in one shot. The resulting workflow is published as version 1 — there
is no prior published version to protect, so we skip the draft step.

This tool is a THIN MCP wrapper: it resolves the caller via
`authenticate_mcp_request()` and delegates to
`api.services.workflow.authoring.create_workflow_from_sdk`, which owns the
parse→DTO→graph→trigger-paths→persist flow and returns the structured
result. The service's `error_code` values are documented in this tool's
docstring (the description shipped to the LLM via `tools/list`); keep the
two in sync — `test_mcp_instructions_drift.py` enforces it.
"""

from __future__ import annotations

from typing import Any

from api.mcp_server.auth import authenticate_mcp_request
from api.mcp_server.tracing import traced_tool
from api.services.workflow.authoring import create_workflow_from_sdk


@traced_tool
async def create_workflow(code: str) -> dict[str, Any]:
    """Parse SDK TypeScript and create a new published workflow.

    `code` is TypeScript source using `@dograh/sdk`. The workflow name
    comes from `new Workflow({ name: "..." })` — it is required.

    Example code:
        import { Workflow } from "@dograh/sdk";
        import { startCall, endCall } from "@dograh/sdk/typed";

        const wf = new Workflow({ name: "lead_qualification" });
        const greeting = wf.addTyped(startCall({ name: "Greeting", prompt: "Hi!" }));
        const done     = wf.addTyped(endCall({ name: "Done", prompt: "Bye." }));
        wf.edge(greeting, done, { label: "done", condition: "conversation complete" });

    On success the new workflow is published as version 1. Use
    `save_workflow(workflow_id, code)` for subsequent edits — those go to
    a draft.

    On failure the result has `created: false`, a machine-readable
    `error_code`, and a human-readable `error` (with file:line:column
    where the problem is locatable). Resubmit the full corrected source —
    patches are not accepted. Possible `error_code` values:
    - `parse_error` — disallowed construct or malformed TypeScript.
    - `validation_error` — node data failed spec validation (unknown
      field, missing required, wrong type, option out of range).
    - `schema_validation` — wire-format (DTO) rejection; rare.
    - `graph_validation` — structural rule broken (e.g. no start node,
      unreachable node, edge to/from the wrong node type).
    - `missing_name` — `new Workflow({ name })` is absent or empty; the
      name is required and there is no prior workflow to fall back to.
    - `trigger_path_conflict` — a trigger node's path is already used by
      another workflow in this organization; rename it and resubmit.
    - `bridge_error` — internal/transient; retry once, then surface it.
    """
    user = await authenticate_mcp_request()
    return await create_workflow_from_sdk(code, user)
