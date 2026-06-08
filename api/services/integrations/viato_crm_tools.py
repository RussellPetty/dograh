"""Pre-built Viato CRM tools for the Viato Voice (Clerk) deployment.

These are ordinary Dograh ``http_api`` tools — not a new pipeline tool type — so
they flow through the existing tool picker, function-schema generation, and
``execute_http_tool`` runtime untouched. Each tool POSTs to a ``/api/voice/crm/*``
endpoint on the Viato CRM, authenticated with a per-org bearer credential whose
secret is ``DOGRAH_INTERNAL_API_SECRET`` (the CRM verifies it).

The agent fills the business fields (first_name, note, stage, ...). Dograh injects
the *call context* (which contact / org / user the call is for) via
``preset_parameters`` rendered from the run's ``initial_context``:

    contact_id    <- {{initial_context.contact_id}}
    crm_org_id    <- {{initial_context.crm_org_id}}
    clerk_user_id <- {{initial_context.clerk_user_id}}

These three are marked ``required=False`` so a misconfigured call (missing
context) does not hard-fail the tool before it can even reach the CRM — the CRM
returns a clean error instead.

``ensure_viato_crm_tools`` idempotently seeds the credential + the four tool rows
for an organization. It is called lazily from the tools-list route when
``AUTH_PROVIDER == "clerk"``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from loguru import logger

from api.constants import DOGRAH_INTERNAL_API_SECRET
from api.db import db_client
from api.enums import ToolCategory, WebhookCredentialType

# Name of the shared bearer credential that authenticates Dograh -> Viato CRM
# internal calls. One per org (credential names are unique per organization).
VIATO_CRM_CREDENTIAL_NAME = "Viato CRM (internal)"

# Default request timeout for CRM writes. CRM mutations fan out to triggers /
# automations, so give them more headroom than the 5s tool default.
_TIMEOUT_MS = 15000


def _viato_crm_base_url() -> str:
    """Resolve the Viato CRM base URL (no trailing slash).

    Read at call time (not import time) so the value can be configured per
    deployment without reimporting the module.
    """
    raw = os.getenv("VIATO_CRM_URL", "") or ""
    return raw.rstrip("/")


def _action_url(action: str) -> str:
    return f"{_viato_crm_base_url()}/api/voice/crm/{action}"


# Preset parameters shared by every CRM tool: they pin the call's CRM context so
# the tool always acts on the CALLED contact, never on something the LLM guessed.
def _context_preset_parameters() -> List[Dict[str, Any]]:
    return [
        {
            "name": "contact_id",
            "type": "string",
            "value_template": "{{initial_context.contact_id}}",
            "required": False,
        },
        {
            "name": "crm_org_id",
            "type": "string",
            "value_template": "{{initial_context.crm_org_id}}",
            "required": False,
        },
        {
            "name": "clerk_user_id",
            "type": "string",
            "value_template": "{{initial_context.clerk_user_id}}",
            "required": False,
        },
    ]


def _http_definition(
    *,
    action: str,
    parameters: List[Dict[str, Any]],
    credential_uuid: str | None,
) -> Dict[str, Any]:
    """Build an http_api tool definition dict for a CRM action."""
    config: Dict[str, Any] = {
        "method": "POST",
        "url": _action_url(action),
        "parameters": parameters,
        "preset_parameters": _context_preset_parameters(),
        "timeout_ms": _TIMEOUT_MS,
    }
    if credential_uuid:
        config["credential_uuid"] = credential_uuid
    else:
        # Fallback when no credential could be created: embed the bearer header
        # statically. execute_http_tool merges config["headers"] into the request.
        config["headers"] = {
            "Authorization": f"Bearer {DOGRAH_INTERNAL_API_SECRET}"
        }
    return {
        "schema_version": 1,
        "type": "http_api",
        "config": config,
    }


def _tool_specs(credential_uuid: str | None) -> List[Dict[str, Any]]:
    """Return the four CRM tool specs (name/description/icon/definition).

    Field params are the values the LLM fills during the call. Optionality is
    chosen so the agent can call the tool with whatever it learned without the
    preset/required machinery rejecting a partial update.
    """
    return [
        {
            "name": "Create Contact",
            "description": (
                "Create a NEW contact in the CRM for the person on this call. "
                "Use only when the caller is not already a known contact. "
                "Provide at least a first or last name; include email, phone, "
                "company, and any notes you have gathered."
            ),
            "icon": "user-plus",
            "icon_color": "#10B981",
            "definition": _http_definition(
                action="create-contact",
                parameters=[
                    {
                        "name": "first_name",
                        "type": "string",
                        "description": "Contact's first name.",
                        "required": False,
                    },
                    {
                        "name": "last_name",
                        "type": "string",
                        "description": "Contact's last name.",
                        "required": False,
                    },
                    {
                        "name": "email",
                        "type": "string",
                        "description": "Contact's email address.",
                        "required": False,
                    },
                    {
                        "name": "phone",
                        "type": "string",
                        "description": "Contact's phone number.",
                        "required": False,
                    },
                    {
                        "name": "company",
                        "type": "string",
                        "description": "Contact's company or organization.",
                        "required": False,
                    },
                    {
                        "name": "notes",
                        "type": "string",
                        "description": (
                            "A note to attach to the new contact summarizing "
                            "what you learned on the call."
                        ),
                        "required": False,
                    },
                ],
                credential_uuid=credential_uuid,
            ),
        },
        {
            "name": "Update Contact",
            "description": (
                "Update the contact who is on this call. Use to correct or fill "
                "in details (name, email, phone, company) that the caller "
                "provides. Only send the fields that changed."
            ),
            "icon": "user-pen",
            "icon_color": "#3B82F6",
            "definition": _http_definition(
                action="update-contact",
                parameters=[
                    {
                        "name": "first_name",
                        "type": "string",
                        "description": "Updated first name.",
                        "required": False,
                    },
                    {
                        "name": "last_name",
                        "type": "string",
                        "description": "Updated last name.",
                        "required": False,
                    },
                    {
                        "name": "email",
                        "type": "string",
                        "description": "Updated email address.",
                        "required": False,
                    },
                    {
                        "name": "phone",
                        "type": "string",
                        "description": "Updated phone number.",
                        "required": False,
                    },
                    {
                        "name": "company",
                        "type": "string",
                        "description": "Updated company or organization.",
                        "required": False,
                    },
                    {
                        "name": "notes",
                        "type": "string",
                        "description": (
                            "Optional note to attach to the contact alongside "
                            "the field updates."
                        ),
                        "required": False,
                    },
                ],
                credential_uuid=credential_uuid,
            ),
        },
        {
            "name": "Update Deal",
            "description": (
                "Update the active deal associated with the contact on this "
                "call (e.g. move it to a new stage, set its value, retitle it, "
                "or add deal notes). Use after the caller commits to a next "
                "step or shares deal details."
            ),
            "icon": "trending-up",
            "icon_color": "#8B5CF6",
            "definition": _http_definition(
                action="update-deal",
                parameters=[
                    {
                        "name": "stage",
                        "type": "string",
                        "description": (
                            "Name of the pipeline stage to move the deal to "
                            "(e.g. 'Appointment Booked', 'Pre-Approved'). "
                            "Match an existing stage in the deal's pipeline."
                        ),
                        "required": False,
                    },
                    {
                        "name": "value",
                        "type": "number",
                        "description": "Monetary value of the deal, in dollars.",
                        "required": False,
                    },
                    {
                        "name": "title",
                        "type": "string",
                        "description": "New name/title for the deal.",
                        "required": False,
                    },
                    {
                        "name": "notes",
                        "type": "string",
                        "description": "Deal notes to record from this call.",
                        "required": False,
                    },
                ],
                credential_uuid=credential_uuid,
            ),
        },
        {
            "name": "Add Note",
            "description": (
                "Add a note to the contact on this call. Use to log a summary "
                "of the conversation, the outcome, or any follow-up details."
            ),
            "icon": "sticky-note",
            "icon_color": "#F59E0B",
            "definition": _http_definition(
                action="add-note",
                parameters=[
                    {
                        "name": "note",
                        "type": "string",
                        "description": "The note text to add to the contact.",
                        "required": True,
                    },
                ],
                credential_uuid=credential_uuid,
            ),
        },
    ]


async def _ensure_credential(
    organization_id: int, created_by_user_id: int
) -> str | None:
    """Find-or-create the org's Viato CRM bearer credential. Returns its UUID.

    Idempotent: matches by the unique per-org credential name. Returns ``None``
    (and the tools fall back to a static header) if no internal secret is
    configured or creation fails.
    """
    if not DOGRAH_INTERNAL_API_SECRET:
        logger.warning(
            "DOGRAH_INTERNAL_API_SECRET is not set; Viato CRM tools will be "
            "seeded without a credential (unauthenticated)."
        )
        return None

    try:
        existing = await db_client.get_credentials_for_organization(organization_id)
        for cred in existing:
            if cred.name == VIATO_CRM_CREDENTIAL_NAME:
                return cred.credential_uuid

        credential = await db_client.create_credential(
            organization_id=organization_id,
            user_id=created_by_user_id,
            name=VIATO_CRM_CREDENTIAL_NAME,
            description="Bearer secret for Dograh -> Viato CRM internal calls.",
            credential_type=WebhookCredentialType.BEARER_TOKEN.value,
            credential_data={"token": DOGRAH_INTERNAL_API_SECRET},
        )
        return credential.credential_uuid
    except Exception as e:  # noqa: BLE001
        # Unique-constraint races (another worker created it) land here too —
        # re-read and use the existing row rather than failing the seed.
        logger.warning(
            f"Viato CRM credential ensure failed for org {organization_id}: {e}"
        )
        try:
            existing = await db_client.get_credentials_for_organization(
                organization_id
            )
            for cred in existing:
                if cred.name == VIATO_CRM_CREDENTIAL_NAME:
                    return cred.credential_uuid
        except Exception:  # noqa: BLE001
            pass
        return None


async def ensure_viato_crm_tools(
    organization_id: int, created_by_user_id: int
) -> None:
    """Idempotently seed the 4 Viato CRM tools (+ credential) for an org.

    Skips any tool whose name already exists for the org (across all statuses),
    so re-running is a no-op and a user who renamed/archived a seeded tool is
    not given a duplicate. Best-effort: individual failures are logged, never
    raised, so the caller's tool listing is never broken.
    """
    credential_uuid = await _ensure_credential(organization_id, created_by_user_id)

    try:
        existing_tools = await db_client.get_tools_for_organization(
            organization_id,
            # Include archived/draft so we never re-create a tool the user
            # intentionally archived.
            status="active,archived,draft",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"Could not list tools for org {organization_id} while seeding "
            f"Viato CRM tools: {e}"
        )
        existing_tools = []

    existing_names = {tool.name for tool in existing_tools}

    for spec in _tool_specs(credential_uuid):
        if spec["name"] in existing_names:
            continue
        try:
            await db_client.create_tool(
                organization_id=organization_id,
                user_id=created_by_user_id,
                name=spec["name"],
                definition=spec["definition"],
                category=ToolCategory.HTTP_API.value,
                description=spec["description"],
                icon=spec["icon"],
                icon_color=spec["icon_color"],
            )
            logger.info(
                f"Seeded Viato CRM tool '{spec['name']}' for org {organization_id}"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Failed to seed Viato CRM tool '{spec['name']}' for org "
                f"{organization_id}: {e}"
            )
