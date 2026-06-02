"""Microsoft Entra ID (Microsoft 365) authentication for the Instagram MCP server.

The server is its own OAuth 2.1 **Authorization Server** that *brokers* login to
Entra ID. MCP clients (Claude) talk only to this server: they dynamically register
(DCR), run the PKCE authorization-code flow, and receive tokens this server issues.
Behind the scenes the broker redirects the user to Entra for the actual M365 sign-in,
validates the resulting Entra token (signature/issuer/audience/tenant + required app
role), and then mints its own opaque access/refresh tokens.

This authenticates *who* may call the server. It does not touch Instagram: the shared
Instagram access token stays server-side in ``InstagramClient`` and is never exposed.
The Entra client secret is used only for the server-side back-channel exchange; it is
never seen by users or MCP clients.

``EntraTokenVerifier`` (below) is reused by the broker to validate the Entra token in
the callback. State is held **in memory** (single replica; a restart forces re-login).
"""

import asyncio
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import structlog
from jwt import PyJWKClient
from jwt import decode as jwt_decode
from jwt.exceptions import PyJWTError
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenVerifier,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl

from .config import InstagramMCPSettings

logger = structlog.get_logger(__name__)


class EntraTokenVerifier(TokenVerifier):
    """Verify Entra ID JWT access tokens and enforce the access app role."""

    def __init__(self, settings: InstagramMCPSettings):
        self.settings = settings
        self.tenant_id = settings.entra_tenant_id
        self.client_id = settings.entra_client_id
        self.audience = settings.entra_audience_value
        self.required_role = settings.entra_required_role
        self.issuer = settings.entra_issuer

        # Entra ID v2.0 signing keys. PyJWKClient caches keys and refreshes on
        # an unknown ``kid`` (e.g. after Microsoft rotates signing keys).
        jwks_uri = (
            f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"
        )
        self._jwk_client = PyJWKClient(jwks_uri, cache_keys=True)

        # Accept either the App ID URI (api://<client_id>) or the bare client id,
        # since Entra issues both forms depending on how the scope is requested.
        self._valid_audiences = [a for a in {self.audience, self.client_id} if a]

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """Return an ``AccessToken`` for a valid, authorized token, else ``None``.

        Returning ``None`` makes the MCP SDK respond 401 with a ``WWW-Authenticate``
        challenge pointing clients at our protected-resource metadata.
        """
        try:
            # JWKS lookup is synchronous (urllib under the hood); run off the loop.
            signing_key = await asyncio.to_thread(
                self._jwk_client.get_signing_key_from_jwt, token
            )
            claims = jwt_decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._valid_audiences,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except PyJWTError as exc:
            logger.warning("JWT validation failed", error=str(exc))
            return None
        except Exception as exc:  # JWKS fetch / network errors
            logger.warning("Token verification error", error=str(exc))
            return None

        # Defense in depth: pin the tenant id even though issuer already encodes it.
        if self.tenant_id and claims.get("tid") != self.tenant_id:
            logger.warning("Token tenant mismatch", tid=claims.get("tid"))
            return None

        # Single-gate authorization: caller must hold the required app role.
        roles = claims.get("roles") or []
        if self.required_role and self.required_role not in roles:
            logger.warning(
                "Token missing required role",
                required=self.required_role,
                subject=claims.get("oid") or claims.get("sub"),
            )
            return None

        scp = claims.get("scp")
        scopes = scp.split(" ") if isinstance(scp, str) and scp else list(roles)

        return AccessToken(
            token=token,
            client_id=claims.get("azp") or claims.get("appid") or "",
            scopes=scopes,
            expires_at=claims.get("exp"),
            resource=self.audience,
            subject=claims.get("oid") or claims.get("sub"),
            claims=claims,
        )


DEFAULT_SCOPE = "mcp"


class IGMcpAuthProvider(OAuthAuthorizationServerProvider):
    """OAuth 2.1 Authorization Server that brokers login to Entra ID.

    Implements the MCP SDK provider protocol. The SDK mounts ``/authorize``,
    ``/token``, ``/register`` and the AS metadata, and verifies PKCE (S256) itself,
    then calls these methods. ``load_access_token`` doubles as the resource-server
    check for ``/mcp``. All state is in-memory (see module docstring).
    """

    def __init__(self, settings: InstagramMCPSettings):
        self.settings = settings
        self.verifier = EntraTokenVerifier(settings)
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._txns: dict[str, dict[str, Any]] = {}  # entra-leg state -> pending login
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

    # --- Dynamic client registration ---------------------------------------
    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    # --- Authorize: redirect the user to Entra for M365 login --------------
    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        state = secrets.token_urlsafe(32)
        self._txns[state] = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "client_state": params.state,
            "code_challenge": params.code_challenge,
            "scopes": params.scopes or [DEFAULT_SCOPE],
            "resource": params.resource,
            "created_at": time.time(),
        }
        query = urlencode(
            {
                "client_id": self.settings.entra_client_id,
                "response_type": "code",
                "redirect_uri": self.settings.entra_redirect_uri,
                "scope": f"openid profile {self.settings.entra_login_scope_full}",
                "state": state,
                "response_mode": "query",
            }
        )
        return f"{self.settings.entra_authorize_endpoint}?{query}"

    async def handle_entra_callback(
        self,
        code: Optional[str],
        state: Optional[str],
        error: Optional[str] = None,
    ) -> Optional[str]:
        """Complete the Entra leg. Returns the Claude redirect URL, or None if the
        login transaction is unknown/expired (the route should then 400)."""
        txn = self._txns.pop(state or "", None)
        if txn is None:
            logger.warning("Entra callback with unknown state")
            return None
        redirect = txn["redirect_uri"]
        client_state = txn["client_state"]

        if error or not code:
            logger.warning("Entra login returned an error", error=error)
            return construct_redirect_uri(
                redirect, error=error or "access_denied", state=client_state
            )

        entra_token = await self._redeem_entra_code(code)
        access = await self.verifier.verify_token(entra_token) if entra_token else None
        if access is None:
            # Authenticated but not authorized (missing role) or exchange failed.
            return construct_redirect_uri(
                redirect,
                error="access_denied",
                error_description="not authorized for this server",
                state=client_state,
            )

        our_code = secrets.token_urlsafe(32)
        self._auth_codes[our_code] = AuthorizationCode(
            code=our_code,
            scopes=txn["scopes"],
            expires_at=time.time() + self.settings.oauth_code_ttl,
            client_id=txn["client_id"],
            code_challenge=txn["code_challenge"],
            redirect_uri=AnyUrl(redirect),
            redirect_uri_provided_explicitly=txn["redirect_uri_provided_explicitly"],
            resource=txn["resource"],
            subject=access.subject,
        )
        logger.info("Entra login authorized", subject=access.subject)
        return construct_redirect_uri(redirect, code=our_code, state=client_state)

    async def _redeem_entra_code(self, code: str) -> Optional[str]:
        """Back-channel: exchange the Entra auth code for an Entra access token."""
        data = {
            "client_id": self.settings.entra_client_id,
            "client_secret": self.settings.entra_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.entra_redirect_uri,
            "scope": self.settings.entra_login_scope_full,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.settings.entra_token_endpoint, data=data)
            if resp.status_code != 200:
                logger.warning(
                    "Entra token exchange failed",
                    status=resp.status_code,
                    body=resp.text[:300],
                )
                return None
            return resp.json().get("access_token")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Entra token exchange error", error=str(exc))
            return None

    # --- Authorization code -> our tokens ----------------------------------
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        ac = self._auth_codes.get(authorization_code)
        if ac is None or ac.client_id != client.client_id:
            return None
        if ac.expires_at < time.time():
            self._auth_codes.pop(authorization_code, None)
            return None
        return ac

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # PKCE (S256) was already verified by the SDK's /token handler.
        self._auth_codes.pop(authorization_code.code, None)
        return self._issue_tokens(
            client.client_id,
            authorization_code.scopes,
            authorization_code.subject,
            authorization_code.resource,
        )

    def _issue_tokens(
        self,
        client_id: str,
        scopes: list[str],
        subject: Optional[str],
        resource: Optional[str],
    ) -> OAuthToken:
        now = time.time()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(now + self.settings.oauth_token_ttl),
            resource=resource,
            subject=subject,
        )
        self._refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(now + self.settings.oauth_refresh_ttl),
            subject=subject,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.settings.oauth_token_ttl,
            scope=" ".join(scopes) or None,
            refresh_token=refresh,
        )

    # --- Refresh -----------------------------------------------------------
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        rt = self._refresh_tokens.get(refresh_token)
        if rt is None or rt.client_id != client.client_id:
            return None
        if rt.expires_at and rt.expires_at < time.time():
            self._refresh_tokens.pop(refresh_token, None)
            return None
        return rt

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate both tokens (SDK guidance); preserve the resource owner.
        self._refresh_tokens.pop(refresh_token.token, None)
        return self._issue_tokens(
            client.client_id,
            scopes or refresh_token.scopes,
            refresh_token.subject,
            None,
        )

    # --- Resource-server check for /mcp (via ProviderTokenVerifier) ---------
    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        at = self._access_tokens.get(token)
        if at is None:
            return None
        if at.expires_at and at.expires_at < time.time():
            self._access_tokens.pop(token, None)
            return None
        return at

    async def revoke_token(self, token: Any) -> None:
        self._access_tokens.pop(token.token, None)
        self._refresh_tokens.pop(token.token, None)


def build_auth_settings(settings: InstagramMCPSettings) -> AuthSettings:
    """Build ``AuthSettings`` for Authorization-Server mode.

    ``issuer_url`` is THIS server (we are the AS), and DCR is enabled so MCP clients
    self-register — no client ID/secret distributed to users. Login is brokered to
    Entra ID inside the provider's ``authorize``/callback.
    """
    if not settings.server_public_url:
        raise ValueError("server_public_url is required to build AuthSettings")
    base = AnyHttpUrl(settings.server_public_url)
    return AuthSettings(
        issuer_url=base,
        resource_server_url=base,
        required_scopes=None,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[DEFAULT_SCOPE],
            default_scopes=[DEFAULT_SCOPE],
        ),
    )
