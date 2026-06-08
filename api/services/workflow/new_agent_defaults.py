"""Starting values applied to a brand-NEW agent at create time.

These mirror the frontend defaults (``ui/src/components/workflow/
CreateWorkflowButton.tsx`` for the start-node fields and
``ui/src/types/workflow-configurations.ts`` for the workflow-level config) so a
voice agent created via the UI, the public API, or the super-agent all begins
with the same Viato Voice defaults. Every value here is a *starting point* the
user can change after creation.

Scope: only applied when a NEW workflow is minted (the
``POST /workflow/create/definition`` endpoint). Existing workflows are never
touched — the helpers only fill a field when it is absent, so an explicit
author choice (e.g. an API caller that ships its own greeting) always wins.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

# Literal default greeting the user then edits (no interpolation here — the
# placeholder text is shown verbatim in the editor).
DEFAULT_GREETING_TEXT = "Hi! This is (name) with (company's name of the user)."


def default_workflow_configurations() -> Dict[str, Any]:
    """Workflow-level config seeded onto a new agent.

    Kept in sync with the frontend ``DEFAULT_WORKFLOW_CONFIGURATIONS`` /
    ``DEFAULT_VOICEMAIL_DETECTION_CONFIGURATION`` so the runtime (which reads
    the persisted config and treats a missing key as disabled) matches what the
    settings UI shows.
    """
    return {
        "ambient_noise_configuration": {
            "enabled": True,
            "volume": 0.3,
        },
        "voicemail_detection": {
            "enabled": True,
            "use_workflow_llm": True,
            "long_speech_timeout": 8.0,
        },
    }


def merged_default_configurations(
    existing: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Layer the new-agent config defaults under any caller-supplied config.

    Caller-provided keys win; we only fill the ones a brand-new agent would
    otherwise be missing (ambient noise + voicemail detection).
    """
    merged = default_workflow_configurations()
    if existing:
        merged.update(existing)
    return merged


def _is_start_node(node: Dict[str, Any]) -> bool:
    if node.get("type") == "startCall":
        return True
    data = node.get("data") or {}
    return bool(data.get("is_start"))


def apply_new_agent_node_defaults(
    workflow_definition: Optional[Dict[str, Any]],
    *,
    tool_uuids: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Return a copy of ``workflow_definition`` with new-agent node defaults.

    On the start node, fills (only when absent so an explicit author value
    wins):
      - ``greeting`` / ``greeting_type`` -> the literal default greeting text
      - ``allow_interrupt`` -> True
      - ``tool_uuids`` -> all of the org's active tool uuids (when provided and
        the node doesn't already declare tools)

    Returns the input unchanged (deep-copied) when there's nothing to do.
    """
    if not workflow_definition or not workflow_definition.get("nodes"):
        return workflow_definition

    definition = copy.deepcopy(workflow_definition)
    for node in definition["nodes"]:
        if not _is_start_node(node):
            continue
        data = node.setdefault("data", {})

        if not data.get("greeting"):
            data["greeting"] = DEFAULT_GREETING_TEXT
            data.setdefault("greeting_type", "text")

        if "allow_interrupt" not in data:
            data["allow_interrupt"] = True

        # Turn all tools on by default — only when the node hasn't already
        # declared a tool selection (don't override an explicit author choice).
        if tool_uuids and not data.get("tool_uuids"):
            data["tool_uuids"] = list(tool_uuids)

    return definition
