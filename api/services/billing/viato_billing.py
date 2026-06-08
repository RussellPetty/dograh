"""Billing bridge to the Viato CRM token system (embedded "Viato Voice" mode).

When ``VIATO_BILLING_ENABLED`` is set, dograh:
  - gates calls on the caller's CRM token balance (pre-call), and
  - reports completed-call usage so the CRM debits tokens (post-call),
both authenticated with an HS256 JWT signed using ``VOICE_USAGE_WEBHOOK_SECRET``
(verified by the CRM's /api/voice/* routes against the same shared secret).

All functions fail soft: the balance check fails OPEN (don't block calls on a
transient CRM outage) and usage reporting is best-effort (logged, never raised) —
the CRM webhook is idempotent per ``workflow_run_id``.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Optional

import httpx
import jwt
from loguru import logger

from api.db import db_client
from api.db.models import UserModel

_ISSUER = "viato-voice"
_AUDIENCE = "viato-crm"


def is_viato_billing_enabled() -> bool:
    return os.getenv("VIATO_BILLING_ENABLED", "false").lower() == "true"


def _crm_url() -> Optional[str]:
    url = os.getenv("VIATO_CRM_URL")
    return url.rstrip("/") if url else None


def _secret() -> Optional[str]:
    return os.getenv("VOICE_USAGE_WEBHOOK_SECRET")


def _mint_token(claims: dict) -> Optional[str]:
    secret = _secret()
    if not secret:
        logger.error(
            "VOICE_USAGE_WEBHOOK_SECRET not set; cannot reach Viato CRM billing"
        )
        return None
    now = datetime.now(UTC)
    payload = {
        **claims,
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "iat": now,
        "exp": now + timedelta(seconds=60),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


async def _post(path: str, claims: dict) -> Optional[dict]:
    base = _crm_url()
    token = _mint_token(claims)
    if not base or not token:
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base}{path}", json={"token": token}, timeout=10.0
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"Viato billing {path} -> {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        logger.warning(f"Viato billing {path} request failed: {e}")
        return None


async def _org_provider_id(organization_id: Optional[int]) -> Optional[str]:
    if not organization_id:
        return None
    org = await db_client.get_organization_by_id(organization_id)
    return org.provider_id if org else None


async def check_balance_for_user(user: UserModel) -> bool:
    """Pre-call gate. Returns True (allow) unless the CRM reports the balance is too low.

    Fails OPEN (returns True) when billing is misconfigured or the CRM is
    unreachable, so a transient outage never blocks every call.
    """
    clerk_org_id = await _org_provider_id(user.selected_organization_id)
    data = await _post(
        "/api/voice/balance-check",
        {"clerk_user_id": user.provider_id, "clerk_org_id": clerk_org_id},
    )
    if data is None:
        return True  # fail open
    return bool(data.get("allowed", True))


async def fetch_twilio_credentials(user: UserModel) -> Optional[dict]:
    """Resolve which Twilio account this user's "Viato Voice" telephony should use.

    Asks the CRM (which owns the user's phone setup): BYO Twilio users get their
    own account; ViatoPhone / "use our Twilio" users get Viato's shared/managed
    account. Returns ``{account_sid, auth_token, from_numbers, source}`` or
    ``None`` if it can't be resolved. Independent of ``VIATO_BILLING_ENABLED`` —
    telephony auto-config doesn't depend on token billing being on.
    """
    clerk_org_id = await _org_provider_id(user.selected_organization_id)
    return await _post(
        "/api/voice/twilio-credentials",
        {"clerk_user_id": user.provider_id, "clerk_org_id": clerk_org_id},
    )


async def maybe_report_viato_usage(workflow_run, cost_info: dict | None) -> None:
    """Post-call: report the call duration to the CRM so it debits tokens.

    No-op when billing is disabled or there's no billable duration. Best-effort —
    the CRM dedupes by ``workflow_run_id``.
    """
    if not is_viato_billing_enabled() or not cost_info:
        return

    duration = float(cost_info.get("call_duration_seconds") or 0)
    if duration <= 0:
        return

    user = (
        await db_client.get_user_by_id(workflow_run.user_id)
        if workflow_run.user_id
        else None
    )
    clerk_user_id = user.provider_id if user else None
    clerk_org_id = await _org_provider_id(workflow_run.organization_id)

    if not clerk_user_id and not clerk_org_id:
        logger.warning(
            f"Viato usage: no Clerk identity for run {workflow_run.id}; skipping"
        )
        return

    direction = (workflow_run.initial_context or {}).get("direction") or "outbound"
    await _post(
        "/api/voice/usage",
        {
            "clerk_user_id": clerk_user_id,
            "clerk_org_id": clerk_org_id,
            "workflow_run_id": workflow_run.id,
            "direction": direction,
            "duration_seconds": duration,
        },
    )
