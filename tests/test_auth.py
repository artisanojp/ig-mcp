"""Unit tests for the Entra ID OAuth Resource Server token verifier.

These tests sign real RS256 JWTs with a throwaway RSA key and stub the JWKS lookup
so the verifier validates against the matching public key — exercising signature,
issuer, audience, expiry, tenant, and app-role checks without any network calls.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.auth import EntraTokenVerifier
from src.config import InstagramMCPSettings

TENANT_ID = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
AUDIENCE = f"api://{CLIENT_ID}"
ROLE = "IGMCP.Use"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def settings():
    return InstagramMCPSettings(
        instagram_access_token="t",
        facebook_app_id="a",
        facebook_app_secret="s",
        auth_enabled=True,
        entra_tenant_id=TENANT_ID,
        entra_client_id=CLIENT_ID,
        entra_client_secret="broker-secret",
        server_public_url="https://mcp.example.com",
        entra_required_role=ROLE,
    )


@pytest.fixture
def verifier(settings, monkeypatch):
    v = EntraTokenVerifier(settings)

    class _SigningKey:
        key = _PRIVATE_KEY.public_key()

    # Stub the JWKS network lookup with the matching public key.
    monkeypatch.setattr(
        v._jwk_client, "get_signing_key_from_jwt", lambda token: _SigningKey()
    )
    return v


def make_token(**overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "tid": TENANT_ID,
        "oid": "user-object-id",
        "sub": "subject-id",
        "azp": CLIENT_ID,
        "roles": [ROLE],
        "scp": "mcp.access",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return jwt.encode(claims, _PRIVATE_KEY, algorithm="RS256")


async def test_valid_token_is_accepted(verifier):
    result = await verifier.verify_token(make_token())
    assert result is not None
    assert result.subject == "user-object-id"
    assert result.client_id == CLIENT_ID
    assert "mcp.access" in result.scopes


async def test_audience_form_bare_client_id_accepted(verifier):
    # Entra may issue aud as the bare client id rather than the App ID URI.
    result = await verifier.verify_token(make_token(aud=CLIENT_ID))
    assert result is not None


async def test_wrong_audience_rejected(verifier):
    assert await verifier.verify_token(make_token(aud="api://other-app")) is None


async def test_wrong_issuer_rejected(verifier):
    bad_iss = "https://login.microsoftonline.com/other-tenant/v2.0"
    assert await verifier.verify_token(make_token(iss=bad_iss)) is None


async def test_tenant_mismatch_rejected(verifier):
    # Signature/aud/iss valid but tid claim points at a different tenant.
    assert await verifier.verify_token(make_token(tid="99999999")) is None


async def test_missing_required_role_rejected(verifier):
    assert await verifier.verify_token(make_token(roles=[])) is None
    assert await verifier.verify_token(make_token(roles=["SomeOtherRole"])) is None


async def test_expired_token_rejected(verifier):
    past = int(time.time()) - 10
    assert await verifier.verify_token(make_token(exp=past, nbf=past - 3600)) is None


async def test_token_signed_by_wrong_key_rejected(verifier):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "tid": TENANT_ID,
            "roles": [ROLE],
            "exp": now + 3600,
        },
        other_key,
        algorithm="RS256",
    )
    assert await verifier.verify_token(forged) is None


async def test_garbage_token_rejected(verifier):
    assert await verifier.verify_token("not-a-jwt") is None
