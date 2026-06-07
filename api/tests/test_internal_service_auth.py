"""Tests for the internal server-to-server auth path in ``get_user``.

The Viato CRM backend calls Dograh with no live Clerk session: it proves itself
with a shared secret (``DOGRAH_INTERNAL_API_SECRET``) in the Authorization header
and names the Clerk user/org to act as via the ``X-Dograh-Clerk-*`` headers.

These mock ``db_client`` (mirroring the style of the other auth tests) and patch
the secret in the ``depends`` module namespace (it is imported by value).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api.services.auth import depends
from api.services.auth.depends import (
    _handle_clerk_auth,
    _handle_internal_service_auth,
    get_user,
)

SECRET = "super-secret-value"


def _user(user_id: int = 1):
    """A minimal user model stand-in (selected_organization_id starts unset)."""
    return SimpleNamespace(
        id=user_id,
        email=None,
        selected_organization_id=None,
    )


def _empty_cfg():
    """A user-configuration stand-in with no llm/tts/stt set."""
    return SimpleNamespace(llm=None, tts=None, stt=None)


def _mock_db(user, org):
    """Build an AsyncMock db_client that resolves to ``user`` + ``org``.

    ``add_user_to_organization`` / ``update_user_selected_organization`` mutate
    nothing here; the production code sets ``selected_organization_id`` directly,
    so the returned org id is what we assert against.
    """
    db = SimpleNamespace()
    db.get_or_create_user_by_provider_id = AsyncMock(return_value=(user, False))
    db.get_or_create_organization_by_provider_id = AsyncMock(
        return_value=(org, False)
    )
    db.add_user_to_organization = AsyncMock()
    db.update_user_selected_organization = AsyncMock()
    db.update_user_configuration = AsyncMock()
    db.get_user_configurations = AsyncMock(return_value=_empty_cfg())
    db.update_user_email = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# Happy path: valid secret + impersonation headers resolve to the named org.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_secret_with_org_resolves_named_org():
    user = _user()
    org = SimpleNamespace(id=555)
    db = _mock_db(user, org)

    with (
        patch.object(depends, "DOGRAH_INTERNAL_API_SECRET", SECRET),
        patch.object(depends, "db_client", db),
    ):
        result = await get_user(
            authorization=f"Bearer {SECRET}",
            x_dograh_clerk_user_id="user_abc",
            x_dograh_clerk_org_id="org_team_42",
        )

    assert result is user
    assert result.selected_organization_id == 555
    # Org was resolved from the supplied org header (not the fallback).
    db.get_or_create_organization_by_provider_id.assert_awaited_once()
    _, kwargs = db.get_or_create_organization_by_provider_id.await_args
    assert kwargs["org_provider_id"] == "org_team_42"


@pytest.mark.asyncio
async def test_org_fallback_when_no_org_header():
    """No org header -> org_provider_id is ``org_<sub>``."""
    user = _user()
    org = SimpleNamespace(id=777)
    db = _mock_db(user, org)

    with (
        patch.object(depends, "DOGRAH_INTERNAL_API_SECRET", SECRET),
        patch.object(depends, "db_client", db),
    ):
        result = await get_user(
            authorization=f"Bearer {SECRET}",
            x_dograh_clerk_user_id="user_solo",
            x_dograh_clerk_org_id=None,
        )

    assert result.selected_organization_id == 777
    _, kwargs = db.get_or_create_organization_by_provider_id.await_args
    assert kwargs["org_provider_id"] == "org_user_solo"


@pytest.mark.asyncio
async def test_blank_org_header_uses_fallback():
    """A whitespace-only org header is treated as absent -> ``org_<sub>``."""
    user = _user()
    org = SimpleNamespace(id=778)
    db = _mock_db(user, org)

    with patch.object(depends, "db_client", db):
        await _handle_internal_service_auth("user_solo", "   ")

    _, kwargs = db.get_or_create_organization_by_provider_id.await_args
    assert kwargs["org_provider_id"] == "org_user_solo"


# ---------------------------------------------------------------------------
# Org convergence: internal-service and clerk paths produce the SAME
# org_provider_id for the same (sub, org_id), with and without an org.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "org_id, clerk_claims",
    [
        ("org_team_42", {"sub": "user_abc", "org_id": "org_team_42"}),
        (None, {"sub": "user_abc"}),  # no org -> both fall back to org_<sub>
    ],
)
async def test_org_convergence_with_clerk_path(org_id, clerk_claims):
    sub = clerk_claims["sub"]

    # --- internal-service path ---
    isvc_user = _user()
    isvc_org = SimpleNamespace(id=900)
    isvc_db = _mock_db(isvc_user, isvc_org)
    with patch.object(depends, "db_client", isvc_db):
        await _handle_internal_service_auth(sub, org_id)
    _, isvc_kwargs = isvc_db.get_or_create_organization_by_provider_id.await_args

    # --- clerk session path (same claims) ---
    clerk_user = _user()
    clerk_org = SimpleNamespace(id=901)
    clerk_db = _mock_db(clerk_user, clerk_org)
    with (
        patch.object(depends, "db_client", clerk_db),
        patch.object(depends, "verify_clerk_token", return_value=clerk_claims),
    ):
        await _handle_clerk_auth("Bearer fake-jwt")
    _, clerk_kwargs = clerk_db.get_or_create_organization_by_provider_id.await_args

    # Both paths must resolve the org with the IDENTICAL org_provider_id.
    assert isvc_kwargs["org_provider_id"] == clerk_kwargs["org_provider_id"]


# ---------------------------------------------------------------------------
# Secret enforcement.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_secret_rejected():
    db = _mock_db(_user(), SimpleNamespace(id=1))
    with (
        patch.object(depends, "DOGRAH_INTERNAL_API_SECRET", SECRET),
        patch.object(depends, "db_client", db),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_user(
                authorization="Bearer not-the-secret",
                x_dograh_clerk_user_id="user_abc",
            )

    assert exc_info.value.status_code == 401
    assert "Invalid internal service credentials" in str(exc_info.value.detail)
    db.get_or_create_user_by_provider_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_authorization_rejected():
    db = _mock_db(_user(), SimpleNamespace(id=1))
    with (
        patch.object(depends, "DOGRAH_INTERNAL_API_SECRET", SECRET),
        patch.object(depends, "db_client", db),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_user(
                authorization=None,
                x_dograh_clerk_user_id="user_abc",
            )

    assert exc_info.value.status_code == 401
    assert "Invalid internal service credentials" in str(exc_info.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_secret", ["", None])
async def test_empty_secret_env_rejects_no_bypass(empty_secret):
    """An unset/empty DOGRAH_INTERNAL_API_SECRET must never authenticate."""
    db = _mock_db(_user(), SimpleNamespace(id=1))
    with (
        patch.object(depends, "DOGRAH_INTERNAL_API_SECRET", empty_secret),
        patch.object(depends, "db_client", db),
    ):
        # Even presenting the "matching" empty secret must be rejected.
        with pytest.raises(HTTPException) as exc_info:
            await get_user(
                authorization="Bearer ",
                x_dograh_clerk_user_id="user_abc",
            )

    assert exc_info.value.status_code == 401
    assert "Invalid internal service credentials" in str(exc_info.value.detail)
    db.get_or_create_user_by_provider_id.assert_not_awaited()


# ---------------------------------------------------------------------------
# Branch is skipped entirely when the impersonation header is absent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branch_skipped_without_clerk_user_header():
    """Without X-Dograh-Clerk-User-Id the internal branch must not run.

    With AUTH_PROVIDER patched to 'local' and no authorization, control should
    fall through to the OSS handler and 401 from THERE (missing Authorization),
    proving the internal-service branch did not engage.
    """
    db = _mock_db(_user(), SimpleNamespace(id=1))
    with (
        patch.object(depends, "DOGRAH_INTERNAL_API_SECRET", SECRET),
        patch.object(depends, "AUTH_PROVIDER", "local"),
        patch.object(depends, "db_client", db),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_user(authorization=None, x_dograh_clerk_user_id=None)

    # 401 from the OSS path ("Authorization header required"), NOT the internal
    # "Invalid internal service credentials" message.
    assert exc_info.value.status_code == 401
    assert "Invalid internal service credentials" not in str(exc_info.value.detail)
    assert "Authorization header required" in str(exc_info.value.detail)
