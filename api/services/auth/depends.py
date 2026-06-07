import hmac
from typing import Annotated, Optional

import httpx
from fastapi import Header, HTTPException, Query, WebSocket
from loguru import logger
from pydantic import ValidationError

from api.constants import (
    AUTH_PROVIDER,
    DOGRAH_INTERNAL_API_SECRET,
    DOGRAH_MPS_SECRET_KEY,
    MPS_API_URL,
)
from api.db import db_client
from api.db.models import UserModel
from api.enums import PostHogEvent
from api.schemas.user_configuration import UserConfiguration
from api.services.auth.clerk_auth import extract_org_provider_id, verify_clerk_token
from api.services.auth.stack_auth import stackauth
from api.services.configuration.defaults import build_clerk_default_configuration
from api.services.configuration.registry import ServiceProviders
from api.services.posthog_client import capture_event
from api.utils.auth import decode_jwt_token


async def get_user(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    x_dograh_clerk_user_id: Annotated[
        str | None, Header(alias="X-Dograh-Clerk-User-Id")
    ] = None,
    x_dograh_clerk_org_id: Annotated[
        str | None, Header(alias="X-Dograh-Clerk-Org-Id")
    ] = None,
) -> UserModel:
    # ------------------------------------------------------------------
    # Check if API key is provided (takes precedence)
    # ------------------------------------------------------------------
    if x_api_key:
        return await _handle_api_key_auth(x_api_key)

    # ------------------------------------------------------------------
    # Internal service auth (server-to-server, on behalf of a user).
    #
    # The Viato CRM backend calls us with no live Clerk session: it proves
    # itself with a shared secret in the Authorization header and names the
    # Clerk user/org to act as via X-Dograh-Clerk-* headers. A normal Clerk
    # user JWT never carries X-Dograh-Clerk-User-Id, so this branch only ever
    # activates for genuine internal calls.
    # ------------------------------------------------------------------
    if x_dograh_clerk_user_id:
        expected = DOGRAH_INTERNAL_API_SECRET or ""
        presented = (
            authorization[len("Bearer ") :]
            if authorization and authorization.startswith("Bearer ")
            else ""
        )
        # Require a configured, non-empty secret AND a matching presented token.
        # An unset/empty DOGRAH_INTERNAL_API_SECRET must reject (no bypass).
        if not expected or not hmac.compare_digest(presented, expected):
            raise HTTPException(
                status_code=401, detail="Invalid internal service credentials"
            )
        return await _handle_internal_service_auth(
            x_dograh_clerk_user_id, x_dograh_clerk_org_id
        )

    # ------------------------------------------------------------------
    # Check if we're using local (email/password) auth
    # ------------------------------------------------------------------
    if AUTH_PROVIDER == "local":
        return await _handle_oss_auth(authorization)

    # ------------------------------------------------------------------
    # Clerk auth (embedded "Viato Voice" deployment)
    # ------------------------------------------------------------------
    if AUTH_PROVIDER == "clerk":
        return await _handle_clerk_auth(authorization)

    # ------------------------------------------------------------------
    # 1. Validate and fetch the authenticated Stack user
    # ------------------------------------------------------------------

    stack_user = await stackauth.get_user(authorization)
    if stack_user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # ------------------------------------------------------------------
    # 2. Ensure the user has a team (Stack "selected_team_id")
    # ------------------------------------------------------------------

    selected_team_id: str | None = stack_user.get("selected_team_id")
    if not selected_team_id and stack_user.get("selected_team"):
        selected_team_id = stack_user["selected_team"].get("id")

    if not selected_team_id:
        raise HTTPException(status_code=400, detail="No team selected")

    # ------------------------------------------------------------------
    # 3. Persist/Fetch the local User model
    # ------------------------------------------------------------------

    try:
        (
            user_model,
            user_was_created,
        ) = await db_client.get_or_create_user_by_provider_id(stack_user["id"])

        # Sync email from Stack Auth if available and not already set
        stack_email = stack_user.get("primary_email_verified") and stack_user.get(
            "primary_email"
        )
        if stack_email and user_model.email != stack_email:
            await db_client.update_user_email(user_model.id, stack_email)
            user_model.email = stack_email

        if user_was_created:
            capture_event(
                distinct_id=str(stack_user["id"]),
                event=PostHogEvent.SIGNED_UP,
                properties={
                    "auth_provider": "stack",
                },
            )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error while creating user from database {e}"
        )

    # ------------------------------------------------------------------
    # 4. Persist Organization (team) and mapping in local database
    # ------------------------------------------------------------------

    try:
        (
            organization,
            org_was_created,
        ) = await db_client.get_or_create_organization_by_provider_id(
            org_provider_id=selected_team_id, user_id=user_model.id
        )

        # Check if user's selected organization differs from the current organization
        if user_model.selected_organization_id != organization.id:
            await db_client.add_user_to_organization(user_model.id, organization.id)

            # Update user's selected organization
            await db_client.update_user_selected_organization(
                user_model.id, organization.id
            )

            # Update the user_model object to reflect the change
            user_model.selected_organization_id = organization.id

            # Only create default configuration if organization was just created
            # This prevents race conditions where multiple concurrent requests
            # might try to create configurations
            if org_was_created:
                existing_cfg = await db_client.get_user_configurations(user_model.id)
                if not (existing_cfg.llm or existing_cfg.tts or existing_cfg.stt):
                    mps_config = await create_user_configuration_with_mps_key(
                        user_model.id, organization.id, stack_user["id"]
                    )
                    if mps_config:
                        await db_client.update_user_configuration(
                            user_model.id, mps_config
                        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to map user to organization: {exc}",
        )

    return user_model


async def _handle_oss_auth(authorization: str | None) -> UserModel:
    """
    Handle authentication for OSS deployment mode.
    Validates JWT tokens issued by the email/password auth flow.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    # Remove "Bearer " prefix if present
    token = (
        authorization.replace("Bearer ", "")
        if authorization.startswith("Bearer ")
        else authorization
    )

    if not token:
        raise HTTPException(status_code=401, detail="Invalid authorization token")

    try:
        payload = decode_jwt_token(token)
        user = await db_client.get_user_by_id(int(payload["sub"]))
        if user:
            return user
        raise HTTPException(status_code=401, detail="User not found")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def _map_clerk_identity_to_org(
    user_model: UserModel, org_provider_id: str
) -> None:
    """Resolve + attach the Dograh organization for a Clerk-derived identity.

    Shared by both Clerk-session auth (``_handle_clerk_auth``) and internal
    server-to-server auth (``_handle_internal_service_auth``). Both paths must
    converge on the SAME ``org_provider_id`` (the iframe and the agent acting on
    behalf of the same user/org), so the organization mapping is identical.

    Mutates ``user_model.selected_organization_id`` in place. Seeds default model
    config (Viato-supplied OpenRouter/Deepgram/ElevenLabs keys) only when the org
    is freshly created and has no existing llm/tts/stt config, so we never clobber
    an existing configuration. Raises HTTP 500 on failure.
    """
    try:
        (
            organization,
            org_was_created,
        ) = await db_client.get_or_create_organization_by_provider_id(
            org_provider_id=org_provider_id, user_id=user_model.id
        )

        if user_model.selected_organization_id != organization.id:
            await db_client.add_user_to_organization(user_model.id, organization.id)
            await db_client.update_user_selected_organization(
                user_model.id, organization.id
            )
            user_model.selected_organization_id = organization.id

            if org_was_created:
                existing_cfg = await db_client.get_user_configurations(user_model.id)
                if not (existing_cfg.llm or existing_cfg.tts or existing_cfg.stt):
                    default_cfg = build_clerk_default_configuration()
                    if default_cfg:
                        await db_client.update_user_configuration(
                            user_model.id, default_cfg
                        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to map user to organization: {exc}",
        )


async def _handle_clerk_auth(authorization: str | None) -> UserModel:
    """Authenticate via a Clerk session JWT (embedded "Viato Voice" mode).

    Verifies the token against Clerk's JWKS, then maps the Clerk identity onto a
    Dograh user + organization — mirroring the Stack Auth flow in ``get_user``.
    """
    claims = verify_clerk_token(authorization)
    if not claims:
        raise HTTPException(status_code=401, detail="Unauthorized")

    clerk_user_id = claims.get("sub")
    if not clerk_user_id:
        raise HTTPException(status_code=401, detail="Invalid token: missing subject")

    try:
        (
            user_model,
            user_was_created,
        ) = await db_client.get_or_create_user_by_provider_id(str(clerk_user_id))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error while creating user from database {e}"
        )

    # Sync email from the token when present. Best-effort: another user may
    # already hold this email (unique index ix_users_email_lower), and a collision
    # must NOT break login. update_user_email uses its own session, so a failure
    # there is self-contained and safe to swallow here.
    email = claims.get("email")
    if email and user_model.email != email:
        try:
            await db_client.update_user_email(user_model.id, email)
            user_model.email = email
        except Exception as e:
            logger.warning(
                f"Clerk auth: skipping email sync for user {user_model.id} "
                f"(email '{email}' likely already in use): {e}"
            )

    if user_was_created:
        capture_event(
            distinct_id=str(clerk_user_id),
            event=PostHogEvent.SIGNED_UP,
            properties={"auth_provider": "clerk"},
        )

    # Resolve the organization: prefer the Clerk org (shared workspace), else
    # fall back to a per-user org so single-user setups still work.
    org_provider_id = extract_org_provider_id(claims) or f"org_{clerk_user_id}"
    await _map_clerk_identity_to_org(user_model, org_provider_id)

    return user_model


async def _handle_internal_service_auth(
    clerk_user_id: str, clerk_org_id: str | None
) -> UserModel:
    """Authenticate an internal server-to-server call acting on behalf of a user.

    Used by the Viato CRM backend, which has no live Clerk session: it has
    already proven itself with the shared secret in ``get_user`` and names the
    Clerk user/org to impersonate via the ``X-Dograh-Clerk-*`` headers. This
    mirrors ``_handle_clerk_auth`` but SKIPS JWT verification and email sync
    (the header path carries no JWT and no email).

    The ``org_<sub>`` fallback below MUST match ``_handle_clerk_auth``'s
    ``extract_org_provider_id(claims) or f"org_{clerk_user_id}"`` so the agent
    and the iframe converge on the same Dograh organization for a given user.
    """
    try:
        (
            user_model,
            user_was_created,
        ) = await db_client.get_or_create_user_by_provider_id(str(clerk_user_id))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error while creating user from database {e}"
        )

    if user_was_created:
        capture_event(
            distinct_id=str(clerk_user_id),
            event=PostHogEvent.SIGNED_UP,
            properties={"auth_provider": "internal_service"},
        )

    # Same resolution as the clerk path: prefer the supplied org, else fall back
    # to a per-user org. Keep this expression identical to _handle_clerk_auth.
    org_provider_id = (clerk_org_id or "").strip() or f"org_{clerk_user_id}"
    await _map_clerk_identity_to_org(user_model, org_provider_id)

    return user_model


async def _handle_api_key_auth(api_key: str) -> UserModel:
    """
    Handle authentication via X-API-Key header.
    Returns the user who created the API key with the correct organization context.
    """
    # Validate the API key
    api_key_model = await db_client.validate_api_key(api_key)

    if not api_key_model:
        raise HTTPException(status_code=401, detail="Invalid or expired API key")

    # API key must have a created_by user
    if not api_key_model.created_by:
        raise HTTPException(status_code=401, detail="API key has no associated user")

    # Get the user who created this API key
    user = await db_client.get_user_by_id(api_key_model.created_by)
    if not user:
        raise HTTPException(status_code=401, detail="API key owner not found")

    # Set the organization context to the API key's organization
    user.selected_organization_id = api_key_model.organization_id

    logger.debug(
        f"Authenticated via API key: {api_key_model.key_prefix}... "
        f"(user_id={user.id}, org_id={api_key_model.organization_id})"
    )

    return user


async def create_user_configuration_with_mps_key(
    user_id: int, organization_id: int, user_provider_id: str
) -> Optional[UserConfiguration]:
    """Create user configuration using MPS service key.

    Args:
        user_id: The user's ID
        organization_id: The organization's ID
        user_provider_id: The user's provider ID (for created_by field)

    Returns:
        UserConfiguration with MPS-provided API keys or None if failed
    """

    async with httpx.AsyncClient() as client:
        # Use MPS API URL from constants
        if AUTH_PROVIDER == "local":
            # For local auth mode, create a temporary service key without authentication
            response = await client.post(
                f"{MPS_API_URL}/api/v1/service-keys/",
                json={
                    "name": f"Default Dograh Model Service Key",
                    "description": "Auto-generated key for OSS user",
                    "expires_in_days": 7,  # Short-lived for OSS
                    "created_by": user_provider_id,
                },
                timeout=10.0,
            )
        else:
            # For authenticated mode, use the secret key and organization ID
            if not DOGRAH_MPS_SECRET_KEY:
                logger.warning(
                    "Warning: DOGRAH_MPS_SECRET_KEY not set for authenticated mode"
                )
                raise ValidationError("Missing DOGRAH_MPS_SECRET_KEY in non oss mode")

            response = await client.post(
                f"{MPS_API_URL}/api/v1/service-keys/",
                json={
                    "name": f"Default Dograh Model Service Key",
                    "description": f"Auto-generated key for organization {organization_id}",
                    "organization_id": organization_id,
                    "expires_in_days": 90,  # Longer-lived for authenticated users
                    "created_by": user_provider_id,
                },
                headers={"X-Secret-Key": DOGRAH_MPS_SECRET_KEY},
                timeout=10.0,
            )

        if response.status_code == 200:
            data = response.json()
            service_key = data.get("service_key")

            if service_key:
                # Create configuration JSON for storage in database
                # The service_factory will use this to instantiate actual services
                configuration = {
                    "llm": {
                        "provider": ServiceProviders.DOGRAH.value,
                        "api_key": [service_key],
                        "model": "default",
                    },
                    "tts": {
                        "provider": ServiceProviders.DOGRAH.value,
                        "api_key": [service_key],
                        "model": "default",
                        "voice": "default",
                    },
                    "stt": {
                        "provider": ServiceProviders.DOGRAH.value,
                        "api_key": [service_key],
                        "model": "default",
                    },
                }
                user_config = UserConfiguration(**configuration)
                return user_config
        else:
            logger.warning(
                f"Failed to get MPS service key: {response.status_code} - {response.text}"
            )


async def get_superuser(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> UserModel:
    """
    Dependency to check if the authenticated user is a superuser.
    Raises HTTPException if user is not authenticated or not a superuser.
    """
    user = await get_user(authorization, x_api_key)

    if not user.is_superuser:
        raise HTTPException(
            status_code=403, detail="Access denied. Superuser privileges required."
        )

    return user


async def get_user_ws(
    websocket: WebSocket,
    token: str = Query(None),
    api_key: str = Query(None, alias="api_key"),
) -> UserModel:
    """
    WebSocket authentication dependency.
    Uses token or api_key from query parameters for authentication.
    """
    if not token and not api_key:
        await websocket.close(code=1008, reason="Missing authentication token")
        raise HTTPException(status_code=401, detail="Missing authentication token")

    try:
        # API key takes precedence
        if api_key:
            user = await get_user(None, api_key)
        else:
            # Use the same logic as get_user but with token from query
            authorization = f"Bearer {token}"
            user = await get_user(authorization, None)
        return user
    except HTTPException as e:
        await websocket.close(code=1008, reason=e.detail)
        raise
