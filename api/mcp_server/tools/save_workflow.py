"""MCP tool that accepts LLM-authored SDK TypeScript and saves it as a draft.

This tool is a THIN MCP wrapper: it resolves the caller via
`authenticate_mcp_request()` and delegates to
`api.services.workflow.authoring.save_workflow_draft_from_sdk`, which owns
the parse→DTO→graph→trigger-paths→save-draft flow and returns the
structured result. The published version stays intact, so edits are
rollback-safe.

The service's `error_code` values are documented in this tool's docstring
(the description shipped to the LLM via `tools/list`); keep the two in
sync — `test_mcp_instructions_drift.py` enforces it. All LLM-facing
errors include file:line:column where available so the LLM can correct
its code directly.
"""

from __future__ import annotations

from typing import Any

from api.mcp_server.auth import authenticate_mcp_request
from api.mcp_server.tracing import traced_tool
from api.services.workflow.authoring import save_workflow_draft_from_sdk


@traced_tool
async def save_workflow(workflow_id: int, code: str) -> dict[str, Any]:
    """Parse SDK TypeScript and save the resulting workflow as a draft.

    `code` is TypeScript source using `@dograh/sdk`. Fetch the current
    code first via `get_workflow_code(workflow_id)`, edit it, then pass
    the full updated source here.

    Example code:
        import { Workflow } from "@dograh/sdk";
        import { startCall, endCall } from "@dograh/sdk/typed";

        const wf = new Workflow({ name: "lead_qualification" });
        const greeting = wf.addTyped(startCall({ name: "Greeting", prompt: "Hi!" }));
        const done     = wf.addTyped(endCall({ name: "Done", prompt: "Bye." }));
        wf.edge(greeting, done, { label: "done", condition: "conversation complete" });

    On success the draft version is saved; the published version is
    untouched.

    On failure the result has `saved: false`, a machine-readable
    `error_code`, and a human-readable `error` (with file:line:column
    where the problem is locatable). Resubmit the full corrected source —
    patches are not accepted. Possible `error_code` values:
    - `parse_error` — disallowed construct or malformed TypeScript.
    - `validation_error` — node data failed spec validation (unknown
      field, missing required, wrong type, option out of range).
    - `schema_validation` — wire-format (DTO) rejection; rare.
    - `graph_validation` — structural rule broken (e.g. no start node,
      unreachable node, edge to/from the wrong node type).
    - `bridge_error` — internal/transient; retry once, then surface it.
    """
    user = await authenticate_mcp_request()
    return await save_workflow_draft_from_sdk(workflow_id, code, user)
