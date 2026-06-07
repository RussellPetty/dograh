"""Clerk authentication for the embedded "Viato Voice" deployment.

When ``AUTH_PROVIDER=clerk`` the parent app (Viato CRM) renders the rebranded
UI in an iframe and posts a Clerk session JWT in via ``postMessage``. The UI then
sends it as ``Authorization: Bearer <jwt>`` on every API call. This module verifies
that token against Clerk's published JWKS (RS256); :mod:`api.services.auth.depends`
maps the verified identity onto a Dograh user + organization.

Validation mirrors the standard embedded-Clerk-SSO approach: decode the unverified
payload to read the issuer, check it against an allow-list, then verify the
signature with the issuer's published signing key. Configuration is read from the
environment at call time (so tests can set/patch it without import ordering games):

    CLERK_ISSUER             comma-separated allow-list of issuer URLs (required)
    CLERK_JWKS_URL           optional explicit JWKS URL; defaults to
                             ``{issuer}/.well-known/jwks.json``
    CLERK_AUDIENCE           optional expected ``aud`` claim
    CLERK_AUTHORIZED_PARTIES optional comma-separated allow-list of ``azp`` values
"""

import os
from typing import Optional

import jwt
from jwt import PyJWKClient
from loguru import logger


def _issuers() -> list[str]:
    raw = os.getenv("CLERK_ISSUER", "")
    return [i.strip().rstrip("/") for i in raw.split(",") if i.strip()]


def _jwks_url_for(issuer: str) -> str:
    explicit = os.getenv("CLERK_JWKS_URL")
    if explicit:
        return explicit
    return f"{issuer}/.well-known/jwks.json"


def _audience() -> Optional[str]:
    return os.getenv("CLERK_AUDIENCE") or None


def _authorized_parties() -> list[str]:
    raw = os.getenv("CLERK_AUTHORIZED_PARTIES", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


# One PyJWKClient per JWKS URL. The client caches fetched signing keys internally;
# we just avoid re-instantiating (and re-fetching JWKS) on every request.
_jwks_clients: dict[str, PyJWKClient] = {}


def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    client = _jwks_clients.get(jwks_url)
    if client is None:
        client = PyJWKClient(jwks_url, cache_keys=True)
        _jwks_clients[jwks_url] = client
    return client


def _strip_bearer(token: str) -> str:
    return token[len("Bearer ") :] if token.startswith("Bearer ") else token


def verify_clerk_token(authorization: Optional[str]) -> Optional[dict]:
    """Verify a Clerk-issued JWT and return its claims, or ``None`` if invalid.

    Checks, in order: the issuer is in the ``CLERK_ISSUER`` allow-list; the
    signature against that issuer's JWKS (RS256); expiry; ``aud`` when
    ``CLERK_AUDIENCE`` is set; and ``azp`` when ``CLERK_AUTHORIZED_PARTIES`` is
    set. Never raises — returns ``None`` on any failure so callers can respond 401.
    """
    if not authorization:
        return None

    token = _strip_bearer(authorization).strip()
    if not token:
        return None

    allowed_issuers = _issuers()
    if not allowed_issuers:
        logger.error("CLERK_ISSUER is not configured; refusing all Clerk tokens")
        return None

    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        logger.warning(f"Clerk token: could not decode payload: {e}")
        return None

    issuer = (unverified.get("iss") or "").rstrip("/")
    if issuer not in allowed_issuers:
        logger.warning(f"Clerk token: issuer '{issuer}' not in allow-list")
        return None

    try:
        signing_key = _get_jwks_client(_jwks_url_for(issuer)).get_signing_key_from_jwt(
            token
        )
    except Exception as e:  # PyJWKClientError, network/JSON errors, etc.
        logger.warning(f"Clerk token: failed to resolve signing key: {e}")
        return None

    audience = _audience()
    decode_kwargs: dict = {
        "algorithms": ["RS256"],
        "issuer": issuer,
        "options": {"verify_aud": audience is not None},
    }
    if audience is not None:
        decode_kwargs["audience"] = audience

    try:
        claims = jwt.decode(token, signing_key.key, **decode_kwargs)
    except jwt.PyJWTError as e:
        logger.warning(f"Clerk token: verification failed: {e}")
        return None

    authorized_parties = _authorized_parties()
    if authorized_parties:
        azp = claims.get("azp")
        if azp and azp not in authorized_parties:
            logger.warning(f"Clerk token: azp '{azp}' not authorized")
            return None

    return claims


def extract_org_provider_id(claims: dict) -> Optional[str]:
    """Return a stable organization identifier from Clerk claims, or ``None``.

    Prefers an explicit ``org_id`` (surfaced via a Clerk JWT template) and falls
    back to the active-organization object ``o.id`` used by default Clerk session
    tokens. ``None`` means the user has no active Clerk org; the caller then scopes
    a per-user organization instead.
    """
    org_id = claims.get("org_id")
    if org_id:
        return str(org_id)
    active_org = claims.get("o")
    if isinstance(active_org, dict) and active_org.get("id"):
        return str(active_org["id"])
    return None
