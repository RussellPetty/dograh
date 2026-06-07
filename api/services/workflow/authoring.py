"""SDK-TypeScript authoring service for workflows.

This module owns the *reusable* body of the LLM-authoring flow that was
previously inlined in the MCP tools (`create_workflow`, `save_workflow`,
`get_workflow_code`) plus the node-catalog and voice-prompting-guide
projections used by the discovery tools. Per `api/AGENTS.md` (logic in
`services/`, reusable by routes + mcp_server), both the MCP tools and the
REST routes in `api/routes/workflow.py` delegate here.

Every function takes an already-resolved `user: UserModel` — auth (MCP
HTTP-header auth or REST `Depends(get_user)`) is the caller's job, so the
*authoring* logic stays identical regardless of how the user was resolved.

The create/save flows mirror each other:
    1. Parse via the Node TS validator — AST-only, never executes the code.
    2. Pydantic validation via `ReactFlowDTO.model_validate`.
    3. Graph validation via `WorkflowGraph`.
    4. Persist (create → v1 published; save → new draft).

Each failure path returns a structured dict with a machine-readable
`error_code` (via `_create_error` / `_save_error`) plus a human-readable
`error` and, where the validator located it, per-location `errors`. Those
codes and their meanings are documented BYTE-FOR-BYTE in the MCP tool
docstrings shipped to the LLM via `tools/list`; `test_mcp_instructions_drift.py`
enforces that the codes returned here stay in sync with those docstrings.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from loguru import logger
from pydantic import ValidationError as PydanticValidationError

from api.db import db_client
from api.db.agent_trigger_client import TriggerPathConflictError
from api.db.models import UserModel
from api.enums import PostHogEvent
from api.services.posthog_client import capture_event
from api.services.voice_prompting_guide import (
    Stage,
    build_briefing,
    get_topic,
    list_topic_index,
)
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.layout import reconcile_positions
from api.services.workflow.node_specs import SPEC_VERSION, all_specs, get_spec
from api.services.workflow.trigger_paths import (
    extract_trigger_paths,
    validate_trigger_paths,
)
from api.services.workflow.workflow_graph import WorkflowGraph


# ─── Error result shaping ────────────────────────────────────────────────
#
# Two distinct envelopes: `create` uses `created: false`, `save` uses
# `saved: false`. The error-code STRING LITERALS produced below are the
# authoritative set that `test_mcp_instructions_drift.py` cross-checks
# against the MCP tool docstrings — keep the literals and their wording in
# sync with `create_workflow` / `save_workflow` docstrings.


def _create_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"created": False, "error_code": code, "error": message, **extra}


def _save_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"saved": False, "error_code": code, "error": message, **extra}


def _format_errors(errors: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for e in errors:
        loc = ""
        line = e.get("line")
        col = e.get("column")
        if line is not None:
            loc = f" (line {line}" + (f", col {col}" if col is not None else "") + ")"
        parts.append(f"{e.get('message', '')}{loc}")
    return "\n".join(parts)


# NOTE: `parse_code`/`TsBridgeError` (ts_bridge) and the projection helpers
# live under `api.mcp_server` today. Importing them at module top-level would
# create a cycle (api.mcp_server.__init__ eagerly imports the server, which
# imports the thin create_workflow/save_workflow tools, which import THIS
# service). They are runtime-only here, so we import them lazily inside the
# functions to keep `services/` import-independent of `mcp_server/`.


async def _previous_workflow_json(workflow: Any) -> dict[str, Any] | None:
    """Match the agent-facing read tools' source selection."""
    from api.mcp_server.tools._workflow_projection import (
        select_workflow_projection_source,
    )

    source = await select_workflow_projection_source(workflow)
    return source.payload


# ─── Create ──────────────────────────────────────────────────────────────


async def create_workflow_from_sdk(code: str, user: UserModel) -> dict[str, Any]:
    """Parse SDK TypeScript and create a new published workflow.

    Body of the MCP `create_workflow` tool minus `authenticate_mcp_request()`.
    Returns the SAME structured dict — `created`/`error_code`/`error`/`errors`
    on failure, or the created workflow summary on success.
    """
    from api.mcp_server.ts_bridge import TsBridgeError, parse_code

    # 1. Parse + spec-validate via the Node TS validator.
    try:
        parsed = await parse_code(code)
    except TsBridgeError as e:
        logger.warning(f"ts_bridge failure: {e}")
        return _create_error("bridge_error", str(e))

    if not parsed.get("ok"):
        stage = parsed.get("stage", "parse")
        errs = parsed.get("errors") or []
        code_key = "parse_error" if stage == "parse" else "validation_error"
        return _create_error(code_key, _format_errors(errs), errors=errs)

    payload = parsed["workflow"]
    name = (parsed.get("workflowName") or "").strip()
    if not name:
        return _create_error(
            "missing_name",
            'Workflow name is required. Add `new Workflow({ name: "..." })` to the source.',
        )

    # 1b. New workflow — no prior version to reconcile against; layout
    # places new nodes adjacent to their first incoming neighbor.
    payload = reconcile_positions(payload, None)
    trigger_path_issues = validate_trigger_paths(payload)
    if trigger_path_issues:
        return _create_error(
            "validation_error",
            "\n".join(issue.message for issue in trigger_path_issues),
        )

    # 2. Pydantic shape check (defence in depth — parser is spec-driven).
    try:
        dto = ReactFlowDTO.model_validate(payload)
    except PydanticValidationError as e:
        return _create_error("schema_validation", str(e))

    # 3. Graph-level semantic validation (start-node count, edge shape).
    try:
        WorkflowGraph(dto)
    except (ValueError, Exception) as e:  # WorkflowGraph raises ValueError
        return _create_error("graph_validation", str(e))

    # 4. Reject upfront if any trigger path collides with another workflow's
    # trigger in this org so we don't leave an orphan workflow record.
    trigger_paths = extract_trigger_paths(payload)
    if trigger_paths:
        try:
            await db_client.assert_trigger_paths_available(
                trigger_paths=trigger_paths,
            )
        except TriggerPathConflictError as e:
            return _create_error(
                "trigger_path_conflict", str(e), trigger_paths=e.trigger_paths
            )

    # 5. Persist as a new workflow with v1 published.
    workflow = await db_client.create_workflow(
        name,
        payload,
        user.id,
        user.selected_organization_id,
    )

    capture_event(
        distinct_id=str(user.provider_id),
        event=PostHogEvent.WORKFLOW_CREATED,
        properties={
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "source": "mcp",
            "organization_id": user.selected_organization_id,
        },
    )

    if trigger_paths:
        await db_client.sync_triggers_for_workflow(
            workflow_id=workflow.id,
            organization_id=user.selected_organization_id,
            trigger_paths=trigger_paths,
        )

    return {
        "created": True,
        "workflow_id": workflow.id,
        "name": workflow.name,
        "status": workflow.status,
        "version_number": 1,
        "node_count": len(payload["nodes"]),
        "edge_count": len(payload["edges"]),
    }


# ─── Save (draft) ────────────────────────────────────────────────────────


async def save_workflow_draft_from_sdk(
    workflow_id: int, code: str, user: UserModel
) -> dict[str, Any]:
    """Parse SDK TypeScript and save the resulting workflow as a draft.

    Body of the MCP `save_workflow` tool minus `authenticate_mcp_request()`.
    Returns the SAME structured dict — `saved`/`error_code`/`error`/`errors`
    on failure, or the draft summary on success.
    """
    from api.mcp_server.ts_bridge import TsBridgeError, parse_code

    workflow = await db_client.get_workflow(
        workflow_id, organization_id=user.selected_organization_id
    )
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")

    # 1. Parse + spec-validate via the Node TS validator.
    try:
        parsed = await parse_code(code)
    except TsBridgeError as e:
        logger.warning(f"ts_bridge failure: {e}")
        return _save_error("bridge_error", str(e))

    if not parsed.get("ok"):
        stage = parsed.get("stage", "parse")
        errs = parsed.get("errors") or []
        code_key = "parse_error" if stage == "parse" else "validation_error"
        return _save_error(code_key, _format_errors(errs), errors=errs)

    payload = parsed["workflow"]
    new_name = (parsed.get("workflowName") or "").strip()

    # 1b. Reconcile node positions against the previously-stored workflow.
    # The parser drops positions by design (LLMs don't place nodes well);
    # here we fill them back in from what was there before, and pick
    # approximate placements for newly-introduced nodes.
    payload = reconcile_positions(payload, await _previous_workflow_json(workflow))
    trigger_path_issues = validate_trigger_paths(payload)
    if trigger_path_issues:
        return _save_error(
            "validation_error",
            "\n".join(issue.message for issue in trigger_path_issues),
        )

    # 2. Pydantic shape check (defence in depth — parser is spec-driven).
    try:
        dto = ReactFlowDTO.model_validate(payload)
    except PydanticValidationError as e:
        return _save_error("schema_validation", str(e))

    # 3. Graph-level semantic validation (start-node count, edge shape).
    try:
        WorkflowGraph(dto)
    except (ValueError, Exception) as e:  # WorkflowGraph raises ValueError
        return _save_error("graph_validation", str(e))

    # 4a. If the `new Workflow({ name })` in the edited source differs from
    # the stored name, rename the workflow. Name is a workflow-level field
    # (not versioned), so this takes effect immediately.
    name_changed = bool(new_name) and new_name != workflow.name
    if name_changed:
        await db_client.update_workflow(
            workflow_id=workflow_id,
            name=new_name,
            workflow_definition=None,
            template_context_variables=None,
            workflow_configurations=None,
            organization_id=user.selected_organization_id,
        )

    # 4b. Save as a new draft (existing published version stays intact).
    draft = await db_client.save_workflow_draft(
        workflow_id=workflow_id,
        workflow_definition=payload,
    )

    return {
        "saved": True,
        "workflow_id": workflow_id,
        "version_number": draft.version_number,
        "status": draft.status,
        "node_count": len(payload["nodes"]),
        "edge_count": len(payload["edges"]),
        "name": new_name or workflow.name,
        "renamed": name_changed,
    }


# ─── Read (SDK code projection) ──────────────────────────────────────────


async def get_workflow_sdk_code(workflow_id: int, user: UserModel) -> dict[str, Any]:
    """Return the workflow as SDK TypeScript code the LLM can edit.

    Body of the MCP `get_workflow_code` / `get_workflow` projection minus
    `authenticate_mcp_request()`. Output shape:
        {"workflow_id": int, "name": str, "status": str,
         "version": "draft" | "published" | "legacy",
         "version_number": int | None, "code": "<TS source>"}
    """
    from api.mcp_server.tools._workflow_projection import project_workflow_to_sdk_view
    from api.mcp_server.ts_bridge import TsBridgeError

    workflow = await db_client.get_workflow(
        workflow_id, organization_id=user.selected_organization_id
    )
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")

    try:
        view = await project_workflow_to_sdk_view(workflow)
    except TsBridgeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate code: {e}")

    return {
        "workflow_id": workflow_id,
        "name": view["name"],
        "status": workflow.status,
        "version": view["version"],
        "version_number": view["version_number"],
        "code": view["code"],
    }


# ─── Node-type catalog (auth-light discovery) ────────────────────────────
#
# These wrap the pure `node_specs` registry; they take no user because the
# catalog is identical across organizations. The MCP discovery tools and
# the REST catalog routes both call these so the projection can't drift.


def list_node_types_catalog() -> dict[str, Any]:
    """Every available node type with a brief summary, plus `spec_version`."""
    return {
        "spec_version": SPEC_VERSION,
        "node_types": [
            {
                "name": spec.name,
                "display_name": spec.display_name,
                "description": spec.description,
                "category": spec.category.value,
            }
            for spec in all_specs()
        ],
    }


def get_node_type_catalog(name: str) -> dict[str, Any]:
    """Full authoring schema for one node type. Raises 404 if unknown."""
    spec = get_spec(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown node type: {name!r}")
    return spec.to_mcp_dict()


# ─── Voice-prompting guide (auth-light) ──────────────────────────────────


def build_voice_prompting_guide(
    stage: Optional[str] = None,
    topic: Optional[str] = None,
    node_type: Optional[str] = None,
) -> dict[str, Any]:
    """Staged voice-prompting guidance for authoring workflows.

    Mirrors the MCP `get_voice_prompting_guide` projection (minus auth):
    `topic` → full atom; `stage` → a briefing; neither → a flat index.
    """
    if topic is not None and stage is not None:
        raise ValueError(
            "Pass either `topic` or `stage`, not both. Use `stage` for a "
            "briefing index; use `topic` for full content of one atom."
        )

    if topic is not None:
        atom = get_topic(topic)
        if atom is None:
            available = ", ".join(t["id"] for t in list_topic_index())
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Unknown voice-prompting topic: {topic!r}. "
                    f"Available topics: {available or '(none registered)'}."
                ),
            )
        return atom.to_deep_dict()

    if stage is not None:
        try:
            stage_enum = Stage(stage)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown stage: {stage!r}. "
                    f"Use one of: {', '.join(s.value for s in Stage)}."
                ),
            )
        return build_briefing(stage_enum, node_type=node_type)

    return {
        "topics": list_topic_index(),
        "next": (
            "Call with stage='plan'|'create'|'review' for a briefing, or "
            "topic=<id> for the full content of one atom."
        ),
    }
