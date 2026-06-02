"""Unit tests for the embedded OAuth Authorization Server broker (IGMcpAuthProvider).

These exercise the broker logic directly — DCR, the Entra redirect, the callback
(role gate), code/token exchange, refresh rotation, and token expiry — stubbing the
Entra back-channel and JWT validation (JWT mechanics are covered in test_auth.py).
"""

import time
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from src.auth import IGMcpAuthProvider
from src.config import InstagramMCPSettings

CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"


def aret(value):
    """Return an async function that resolves to `value` (for monkeypatching)."""

    async def _fn(*args, **kwargs):
        return value

    return _fn


@pytest.fixture
def settings():
    return InstagramMCPSettings(
        instagram_access_token="t",
        facebook_app_id="a",
        facebook_app_secret="s",
        auth_enabled=True,
        entra_tenant_id=TENANT,
        entra_client_id=CLIENT,
        entra_client_secret="broker-secret",
        server_public_url="https://mcp.example.com",
    )


@pytest.fixture
def provider(settings):
    return IGMcpAuthProvider(settings)


def make_client(client_id="client-1"):
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl(CLAUDE_REDIRECT)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="mcp",
    )


def make_code(client_id="client-1", subject="user-oid", expires_in=600):
    return AuthorizationCode(
        code="ac",
        scopes=["mcp"],
        expires_at=time.time() + expires_in,
        client_id=client_id,
        code_challenge="chal",
        redirect_uri=AnyUrl(CLAUDE_REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=None,
        subject=subject,
    )


async def start_txn(provider, client, claude_state="cs"):
    params = AuthorizationParams(
        state=claude_state,
        scopes=["mcp"],
        code_challenge="chal",
        redirect_uri=AnyUrl(CLAUDE_REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )
    url = await provider.authorize(client, params)
    return url, parse_qs(urlparse(url).query)["state"][0]


async def test_register_and_get_client(provider):
    c = make_client()
    await provider.register_client(c)
    assert await provider.get_client("client-1") is c
    assert await provider.get_client("unknown") is None


async def test_authorize_redirects_to_entra_and_stores_txn(provider):
    url, state = await start_txn(provider, make_client())
    assert url.startswith(
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
    )
    q = parse_qs(urlparse(url).query)
    assert q["redirect_uri"][0] == "https://mcp.example.com/oauth/entra/callback"
    assert q["client_id"][0] == CLIENT
    assert state in provider._txns
    assert provider._txns[state]["code_challenge"] == "chal"
    assert provider._txns[state]["client_state"] == "cs"


async def test_callback_authorized_mints_code(provider, monkeypatch):
    c = make_client()
    _, state = await start_txn(provider, c)
    monkeypatch.setattr(provider, "_redeem_entra_code", aret("entra-token"))
    monkeypatch.setattr(
        provider.verifier,
        "verify_token",
        aret(
            AccessToken(
                token="entra-token",
                client_id="x",
                scopes=["mcp.access"],
                subject="user-oid",
            )
        ),
    )
    target = await provider.handle_entra_callback(code="entra-code", state=state)
    q = parse_qs(urlparse(target).query)
    assert q["state"][0] == "cs"
    our_code = q["code"][0]
    assert our_code in provider._auth_codes
    assert provider._auth_codes[our_code].subject == "user-oid"
    assert state not in provider._txns  # txn consumed


async def test_callback_missing_role_denied(provider, monkeypatch):
    c = make_client()
    _, state = await start_txn(provider, c)
    monkeypatch.setattr(provider, "_redeem_entra_code", aret("entra-token"))
    monkeypatch.setattr(
        provider.verifier, "verify_token", aret(None)
    )  # role gate fails
    target = await provider.handle_entra_callback(code="entra-code", state=state)
    q = parse_qs(urlparse(target).query)
    assert q["error"][0] == "access_denied"
    assert not provider._auth_codes


async def test_callback_entra_error_propagates(provider):
    c = make_client()
    _, state = await start_txn(provider, c)
    target = await provider.handle_entra_callback(
        code=None, state=state, error="access_denied"
    )
    assert parse_qs(urlparse(target).query)["error"][0] == "access_denied"


async def test_callback_unknown_state_returns_none(provider):
    assert await provider.handle_entra_callback(code="x", state="nope") is None


async def test_exchange_code_issues_and_loads_tokens(provider):
    c = make_client()
    code = make_code()
    provider._auth_codes["ac"] = code
    assert await provider.load_authorization_code(c, "ac") is code

    tok = await provider.exchange_authorization_code(c, code)
    assert tok.access_token and tok.refresh_token
    assert "ac" not in provider._auth_codes  # single-use

    at = await provider.load_access_token(tok.access_token)
    assert at is not None and at.subject == "user-oid"


async def test_refresh_rotates_tokens(provider):
    c = make_client()
    tok = await provider.exchange_authorization_code(c, make_code())
    rt = await provider.load_refresh_token(c, tok.refresh_token)
    assert rt is not None
    new = await provider.exchange_refresh_token(c, rt, ["mcp"])
    assert new.access_token != tok.access_token
    # old refresh token is rotated out
    assert await provider.load_refresh_token(c, tok.refresh_token) is None


async def test_load_access_token_rejects_expired_and_unknown(provider):
    provider._access_tokens["old"] = AccessToken(
        token="old", client_id="c", scopes=[], expires_at=int(time.time()) - 10
    )
    assert await provider.load_access_token("old") is None
    assert await provider.load_access_token("missing") is None


async def test_load_code_rejects_wrong_client_and_expired(provider):
    assert await provider.load_authorization_code(make_client("other"), "ac") is None
    provider._auth_codes["ac"] = make_code(client_id="someone-else")
    assert await provider.load_authorization_code(make_client(), "ac") is None
    provider._auth_codes["exp"] = make_code(expires_in=-5)
    provider._auth_codes["exp"].code = "exp"
    assert await provider.load_authorization_code(make_client(), "exp") is None


async def test_revoke_drops_tokens(provider):
    c = make_client()
    tok = await provider.exchange_authorization_code(c, make_code())
    at = await provider.load_access_token(tok.access_token)
    await provider.revoke_token(at)
    assert await provider.load_access_token(tok.access_token) is None
