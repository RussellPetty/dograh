"""Unit tests for Clerk token verification (embedded "Viato Voice" auth).

These exercise ``verify_clerk_token`` end to end with a real RS256 signature,
mocking only the JWKS lookup so no network is needed.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api.services.auth import clerk_auth
from api.services.auth.clerk_auth import extract_org_provider_id, verify_clerk_token

ISSUER = "https://clerk.example.com"


def _make_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_token(signing_key: rsa.RSAPrivateKey, **claims) -> str:
    payload = {
        "iss": ISSUER,
        "sub": "user_123",
        "iat": datetime.now(UTC) - timedelta(seconds=5),
        "exp": datetime.now(UTC) + timedelta(hours=1),
    }
    payload.update(claims)
    return jwt.encode(payload, signing_key, algorithm="RS256")


@pytest.fixture
def signing_key(monkeypatch):
    """Configure clerk_auth with an in-test issuer + signing key (no network)."""
    key = _make_key()
    monkeypatch.setenv("CLERK_ISSUER", ISSUER)
    for var in ("CLERK_JWKS_URL", "CLERK_AUDIENCE", "CLERK_AUTHORIZED_PARTIES"):
        monkeypatch.delenv(var, raising=False)

    public_key = key.public_key()
    fake_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=public_key)
    )
    monkeypatch.setattr(clerk_auth, "_jwks_clients", {})
    monkeypatch.setattr(clerk_auth, "_get_jwks_client", lambda url: fake_client)
    return key


def test_valid_token_returns_claims(signing_key):
    token = _make_token(signing_key, email="jane@example.com", org_id="org_42")
    claims = verify_clerk_token(f"Bearer {token}")
    assert claims is not None
    assert claims["sub"] == "user_123"
    assert claims["email"] == "jane@example.com"
    assert claims["org_id"] == "org_42"


def test_bearer_prefix_optional(signing_key):
    token = _make_token(signing_key)
    assert verify_clerk_token(token) is not None


def test_expired_token_rejected(signing_key):
    token = _make_token(signing_key, exp=datetime.now(UTC) - timedelta(minutes=1))
    assert verify_clerk_token(f"Bearer {token}") is None


def test_unknown_issuer_rejected(signing_key):
    token = _make_token(signing_key, iss="https://evil.example.com")
    assert verify_clerk_token(f"Bearer {token}") is None


def test_bad_signature_rejected(signing_key):
    # Sign with a different key than the one the verifier resolves from JWKS.
    other_key = _make_key()
    token = _make_token(other_key)
    assert verify_clerk_token(f"Bearer {token}") is None


def test_missing_issuer_config_rejects_all(monkeypatch, signing_key):
    monkeypatch.delenv("CLERK_ISSUER", raising=False)
    token = _make_token(signing_key)
    assert verify_clerk_token(f"Bearer {token}") is None


def test_empty_authorization_returns_none(signing_key):
    assert verify_clerk_token(None) is None
    assert verify_clerk_token("") is None
    assert verify_clerk_token("Bearer ") is None


def test_audience_enforced_when_configured(monkeypatch, signing_key):
    monkeypatch.setenv("CLERK_AUDIENCE", "viato-voice")

    ok = _make_token(signing_key, aud="viato-voice")
    assert verify_clerk_token(f"Bearer {ok}") is not None

    missing = _make_token(signing_key)  # no aud claim
    assert verify_clerk_token(f"Bearer {missing}") is None

    wrong = _make_token(signing_key, aud="something-else")
    assert verify_clerk_token(f"Bearer {wrong}") is None


def test_authorized_parties_enforced_when_configured(monkeypatch, signing_key):
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", "https://app.viato.ai")

    ok = _make_token(signing_key, azp="https://app.viato.ai")
    assert verify_clerk_token(f"Bearer {ok}") is not None

    wrong = _make_token(signing_key, azp="https://evil.example.com")
    assert verify_clerk_token(f"Bearer {wrong}") is None


def test_extract_org_provider_id_prefers_org_id():
    assert extract_org_provider_id({"org_id": "org_42"}) == "org_42"


def test_extract_org_provider_id_falls_back_to_active_org():
    assert extract_org_provider_id({"o": {"id": "org_99"}}) == "org_99"


def test_extract_org_provider_id_none_when_absent():
    assert extract_org_provider_id({"sub": "user_1"}) is None
