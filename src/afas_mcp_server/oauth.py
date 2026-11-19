"""AFAS OAuth 2.0: an OAuth authorization server for MCP clients that delegates the login itself to AFAS.

Two OAuth 2.0 conversations meet here. Towards AFAS the server is a confidential client of an OAuth app connector
and uses the authorization code flow with PKCE. Towards MCP clients it is an authorization server of its own.

An MCP client registers with this server and sends the user's browser to it. The user confirms which application
is asking, logs in at AFAS, and AFAS sends the browser back with an authorization code. The server exchanges that
code for the user's AFAS tokens, keeps them encrypted in its own store, and hands the MCP client tokens of its own.
The AFAS tokens never leave the server.
"""

import asyncio
import base64
import binascii
import hashlib
import html
import json
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit
from weakref import WeakValueDictionary

import httpx2
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl, BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .client import AfasAuthorizationError, Authorization
from .configuration import Settings
from .storage import Kind, Store, fingerprint

CONSENT_PATH = '/oauth/consent'
ACCESS_TOKEN_SECONDS = 3600
AFAS_LOGIN_SECONDS = 30 * 24 * 3600
CONSENT_SECONDS = 600
CODE_SECONDS = 300
CLIENT_SECONDS = 90 * 24 * 3600
EXPIRY_MARGIN_SECONDS = 60
MAX_CLIENTS = 5000
SECRET_BYTES = 48
HTTP_ERROR_THRESHOLD = 400
JWT_PART_COUNT = 3
SUBJECT_CLAIMS = ('preferred_username', 'unique_name', 'upn', 'email', 'name', 'sub')
UNKNOWN_SUBJECT = 'unknown-user'
NO_ERROR_CODE = 'no error code'
BEARER = 'Bearer'
SECURE_COOKIE = '__Host-afas-mcp-login'
PLAIN_COOKIE = 'afas-mcp-login'
ALLOW = 'allow'
PAGE_HEADERS = {
    'Cache-Control': 'no-store',
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
}
PAGE_TEMPLATE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #f4f5f7; color: #1c1e21; margin: 0; padding: 16px; }}
main {{ max-width: 460px; margin: 10vh auto; background: #fff; border-radius: 12px; padding: 28px; }}
h1 {{ font-size: 1.25rem; margin-top: 0; }}
code {{ background: #f0f1f3; padding: 2px 6px; border-radius: 4px; word-break: break-all; }}
form {{ display: flex; gap: 12px; margin-top: 24px; }}
button {{ flex: 1; padding: 12px; border-radius: 8px; border: 1px solid #c5c8ce; font-size: 1rem; cursor: pointer; }}
button[value=allow] {{ background: #0b5cad; border-color: #0b5cad; color: #fff; }}
@media (prefers-color-scheme: dark) {{
body {{ background: #16181c; color: #e8eaed; }}
main {{ background: #22252b; }}
code {{ background: #2e323a; }}
button {{ background: #2e323a; color: #e8eaed; border-color: #4a4f59; }}
}}
</style>
</head>
<body><main>{body}</main></body>
</html>
"""
CONSENT_BODY = """<h1>{title}</h1>
<p>{intro}</p>
<p>{destination} <code>{redirect}</code></p>
<p>{warning}</p>
<form method="post" action="{action}">
<input type="hidden" name="ticket" value="{ticket}">
<input type="hidden" name="browser" value="{browser}">
<button type="submit" name="decision" value="deny">{deny}</button>
<button type="submit" name="decision" value="allow">{allow}</button>
</form>
"""
TEXTS = {
    'nl': {
        'lang': 'nl',
        'title': 'Toegang tot AFAS geven?',
        'intro': '<strong>{client}</strong> wil namens jou gegevens in AFAS Profit gebruiken.',
        'destination': 'Na het inloggen ga je terug naar',
        'warning': 'Ga alleen door als je deze koppeling zelf bent gestart. Je logt hierna in bij AFAS.',
        'allow': 'Doorgaan naar AFAS',
        'deny': 'Annuleren',
        'unknown_client': 'Een onbekende applicatie',
        'error_title': 'Inloggen is niet gelukt',
        'expired': 'Deze inlogpoging is verlopen of al gebruikt. Start de koppeling opnieuw vanuit je applicatie.',
        'browser': 'Deze inlogpoging is in een andere browser gestart. Start de koppeling opnieuw.',
    },
    'en': {
        'lang': 'en',
        'title': 'Give access to AFAS?',
        'intro': '<strong>{client}</strong> wants to use data in AFAS Profit on your behalf.',
        'destination': 'After logging in you return to',
        'warning': 'Only continue if you started this connection yourself. You log in at AFAS next.',
        'allow': 'Continue to AFAS',
        'deny': 'Cancel',
        'unknown_client': 'An unknown application',
        'error_title': 'Login did not succeed',
        'expired': 'This login attempt has expired or was already used. Start the connection again from your app.',
        'browser': 'This login attempt was started in another browser. Start the connection again.',
    },
}


class PendingAuthorization(BaseModel):
    """What an MCP client asked for, kept while the user decides and logs in."""

    client_id: str
    client_name: str | None = None
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    state: str | None = None
    scopes: list[str] = []
    resource: str | None = None


class PendingLogin(PendingAuthorization):
    """A pending authorization the user approved, while their browser is at AFAS."""

    code_verifier: str
    browser: str


class AfasLogin(BaseModel):
    """The AFAS tokens of one logged-in user."""

    access_token: str
    refresh_token: str | None = None
    expires_at: float
    deadline: float
    subject: str


class AfasTokenResponse(BaseModel):
    """The fields of an AFAS token response this server uses."""

    access_token: str
    refresh_token: str | None = None
    expires_in: int = ACCESS_TOKEN_SECONDS


class GrantedCode(AuthorizationCode):
    """An authorization code for an MCP client, tied to the AFAS login it was issued for."""

    grant_id: str


class GrantedRefreshToken(RefreshToken):
    """A refresh token for an MCP client, tied to an AFAS login."""

    grant_id: str


class GrantedAccessToken(AccessToken):
    """An access token for an MCP client, tied to an AFAS login."""

    grant_id: str


def new_secret() -> str:
    """Return a fresh random value with 384 bits of entropy, usable as a code, token, state or PKCE verifier."""
    return secrets.token_urlsafe(SECRET_BYTES)


def code_challenge(verifier: str) -> str:
    """Return the S256 PKCE challenge for a verifier.

    Args:
        verifier: The PKCE code verifier.

    Returns:
        The SHA-256 of the verifier, URL-safe base64 encoded without padding.
    """
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def subject_of(access_token: str) -> str:
    """Return a label for the user an AFAS access token belongs to, for the audit log.

    AFAS documents no user-info endpoint, so this reads the claims of the token when it is a JWT. The token came
    straight from AFAS over TLS, which is why its signature is not checked here.

    Args:
        access_token: The AFAS access token.

    Returns:
        The first identifying claim found, or a placeholder when the token reveals none.
    """
    parts = access_token.split('.')
    if len(parts) != JWT_PART_COUNT:
        return UNKNOWN_SUBJECT
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
    except (binascii.Error, ValueError):
        return UNKNOWN_SUBJECT
    if not isinstance(claims, dict):
        return UNKNOWN_SUBJECT
    return next((str(claims[name]) for name in SUBJECT_CLAIMS if claims.get(name)), UNKNOWN_SUBJECT)


def page(texts: dict[str, str], title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """Render a small standalone HTML page that cannot be framed or cached.

    Args:
        texts: The texts of the page language.
        title: The page title.
        body: The HTML inside the page.
        status_code: The HTTP status.

    Returns:
        The response.
    """
    content = PAGE_TEMPLATE.format(lang=texts['lang'], title=html.escape(title), body=body)
    return HTMLResponse(content, status_code=status_code, headers=PAGE_HEADERS)


def texts_for(language: str) -> dict[str, str]:
    """Return the page texts for an AFAS language such as ``nl-nl``, falling back to English.

    Args:
        language: The configured ``Accept-Language`` value.

    Returns:
        The texts of the consent and error pages.
    """
    return TEXTS.get(language[:2].lower(), TEXTS['en'])


def error_page(texts: dict[str, str], explanation: str) -> HTMLResponse:
    """Tell the user their login attempt cannot continue.

    Args:
        texts: The texts of the page language.
        explanation: The key of the explanation in those texts.

    Returns:
        The page, with status 400.
    """
    body = f'<h1>{texts["error_title"]}</h1><p>{texts[explanation]}</p>'
    return page(texts, texts['error_title'], body, status_code=400)


def error_code(response: httpx2.Response) -> str:
    """Return the OAuth error code of a failed token response, without echoing anything secret.

    Args:
        response: The error response of the AFAS token endpoint.

    Returns:
        The ``error`` field, or a placeholder when the body is not an OAuth error.
    """
    try:
        body = response.json()
    except ValueError:
        return NO_ERROR_CODE
    return str(body.get('error', NO_ERROR_CODE)) if isinstance(body, dict) else NO_ERROR_CODE


class AfasLogins:
    """The server as OAuth 2.0 client of AFAS: exchanging codes, keeping every user's tokens and refreshing them."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Prepare the HTTP client for the AFAS token endpoint.

        Args:
            settings: Settings with the OAuth app connector.
            store: Where the tokens of logged-in users are kept.
            transport: Optional HTTP transport, used by tests to fake AFAS.
            clock: Returns the current time in seconds; replaceable in tests.
        """
        self._logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}')
        self.settings = settings
        self.store = store
        self._clock = clock
        self._client_secret = settings.oauth_client_secret.get_secret_value()
        self._http = httpx2.AsyncClient(timeout=httpx2.Timeout(settings.timeout_seconds), transport=transport)
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""
        await self._http.aclose()

    async def request_tokens(self, grant: dict[str, str]) -> AfasTokenResponse:
        """Ask the AFAS token endpoint for tokens, authenticating as the app connector.

        Args:
            grant: The grant parameters: an authorization code with its verifier, or a refresh token.

        Returns:
            The tokens AFAS issued.

        Raises:
            AfasAuthorizationError: When AFAS rejects the grant or answers with something that is not a token.
        """
        data = {**grant, 'client_id': str(self.settings.oauth_client_id), 'client_secret': self._client_secret}
        response = await self._http.post(self.settings.oauth_token_url, data=data)
        if response.status_code >= HTTP_ERROR_THRESHOLD:
            message = f'AFAS token endpoint answered HTTP {response.status_code}: {error_code(response)}'
            raise AfasAuthorizationError(message)
        try:
            return AfasTokenResponse.model_validate_json(response.content)
        except ValidationError as error:
            message = 'AFAS token endpoint did not return an access token'
            raise AfasAuthorizationError(message) from error

    async def start(self, code: str, code_verifier: str) -> tuple[str, AfasLogin]:
        """Exchange the authorization code AFAS handed a user's browser and keep the resulting tokens.

        Args:
            code: The authorization code from the AFAS redirect.
            code_verifier: The PKCE verifier of the login attempt.

        Returns:
            The id of the new login and the login itself.

        Raises:
            AfasAuthorizationError: When AFAS refuses the code.
        """
        tokens = await self.request_tokens(
            {
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': self.settings.oauth_callback_url,
                'code_verifier': code_verifier,
            }
        )
        now = self._clock()
        grant_id = new_secret()
        login = AfasLogin(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_at=now + tokens.expires_in,
            deadline=now + AFAS_LOGIN_SECONDS,
            subject=subject_of(tokens.access_token),
        )
        self.save(grant_id, login)
        return grant_id, login

    def save(self, grant_id: str, login: AfasLogin) -> None:
        """Store a login until the moment AFAS stops refreshing it.

        Args:
            grant_id: The id of the login.
            login: Its tokens.
        """
        self.store.put(Kind.GRANT, grant_id, login.model_dump(), expires_at=login.deadline, grant_id=grant_id)

    def load(self, grant_id: str) -> AfasLogin | None:
        """Return the AFAS tokens of a login.

        Args:
            grant_id: The login to look up.

        Returns:
            The tokens, or None when the user logged out or the 30 days AFAS allows are over.
        """
        stored = self.store.get(Kind.GRANT, grant_id)
        return AfasLogin.model_validate(stored) if stored else None

    async def authorize_call(self) -> Authorization:
        """Resolve the AFAS credentials of the user making the current MCP request.

        Returns:
            The ``Authorization`` header for AFAS and the user it belongs to.

        Raises:
            AfasAuthorizationError: When the request carries no login, or the AFAS login has ended.
        """
        token = get_access_token()
        if not isinstance(token, GrantedAccessToken):
            message = 'this request is not logged in to AFAS'
            raise AfasAuthorizationError(message)
        login = await self.fresh(token.grant_id)
        return Authorization(header=f'{BEARER} {login.access_token}', subject=login.subject)

    async def fresh(self, grant_id: str) -> AfasLogin:
        """Return a login whose AFAS access token is still valid, refreshing it first when it is about to expire.

        AFAS refresh tokens work once, so concurrent requests of one user wait for a single refresh.

        Args:
            grant_id: The login to use.

        Returns:
            The login with a usable access token.

        Raises:
            AfasAuthorizationError: When the login is gone or AFAS will not refresh it; the user must log in again.
        """
        lock = self._locks.setdefault(grant_id, asyncio.Lock())
        async with lock:
            login = self.load(grant_id)
            if login is None:
                message = 'your AFAS login has ended; reconnect to log in again'
                raise AfasAuthorizationError(message)
            if login.expires_at - EXPIRY_MARGIN_SECONDS > self._clock():
                return login
            return await self.refreshed(grant_id, login)

    async def refreshed(self, grant_id: str, login: AfasLogin) -> AfasLogin:
        """Exchange the AFAS refresh token of a login for new tokens and store them.

        Args:
            grant_id: The login being refreshed.
            login: Its current, expired tokens.

        Returns:
            The login with new tokens.

        Raises:
            AfasAuthorizationError: When AFAS refuses; the login and the MCP tokens issued for it are then removed.
        """
        try:
            # Without a refresh token AFAS rejects the request, which ends the login like any other refusal.
            grant = {'grant_type': 'refresh_token', 'refresh_token': login.refresh_token or ''}
            tokens = await self.request_tokens(grant)
        except AfasAuthorizationError as error:
            self._logger.warning('ending the AFAS login of %s: %s', login.subject, error)
            self.store.delete_grant(grant_id)
            message = 'your AFAS login has expired; reconnect to log in again'
            raise AfasAuthorizationError(message) from error
        renewed = login.model_copy(
            update={
                'access_token': tokens.access_token,
                'refresh_token': tokens.refresh_token or login.refresh_token,
                'expires_at': self._clock() + tokens.expires_in,
            }
        )
        self.save(grant_id, renewed)
        return renewed


class AfasOAuthProvider(OAuthAuthorizationServerProvider[GrantedCode, GrantedRefreshToken, GrantedAccessToken]):
    """The authorization server MCP clients talk to.

    It implements the provider protocol of the MCP SDK and serves the consent page and the AFAS callback. The AFAS
    tokens themselves are handled by ``logins``.
    """

    def __init__(
        self,
        settings: Settings,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Open the token store and prepare the AFAS side of the login.

        Args:
            settings: Settings with the OAuth app connector, public URL, encryption key and database path.
            transport: Optional HTTP transport, used by tests to fake AFAS.
            clock: Returns the current time in seconds; replaceable in tests.
        """
        self._logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}')
        self.settings = settings
        self._clock = clock
        self._texts = texts_for(settings.language)
        self._is_https = urlsplit(settings.public_base_url).scheme == 'https'
        # A `__Host-` cookie cannot be planted by another host of the same domain, but browsers only accept it
        # over https.
        self._cookie = SECURE_COOKIE if self._is_https else PLAIN_COOKIE
        self.store = Store(settings.database_path, settings.encryption_key.get_secret_value(), clock)
        self.logins = AfasLogins(settings, self.store, transport, clock)

    async def aclose(self) -> None:
        """Close the HTTP connection pool and the token store."""
        await self.logins.aclose()
        self.store.close()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Return a registered MCP client.

        Args:
            client_id: The id the client was registered under.

        Returns:
            The client, or None when it is unknown or its registration lapsed.
        """
        stored = self.store.get(Kind.CLIENT, client_id)
        return OAuthClientInformationFull.model_validate(stored) if stored else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Remember an MCP client that registered itself.

        Args:
            client_info: The client metadata, including the id and secret the SDK generated.

        Raises:
            RegistrationError: When the store already holds the maximum number of clients.
        """
        if self.store.count(Kind.CLIENT) >= MAX_CLIENTS:
            description = 'too many registered clients; try again later'
            raise RegistrationError(error='invalid_client_metadata', error_description=description)
        self.remember_client(client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park an authorization request and send the browser to the consent page.

        Args:
            client: The MCP client asking for access.
            params: The validated parameters of its authorization request.

        Returns:
            The URL of the consent page for this request.
        """
        pending = PendingAuthorization(
            client_id=str(client.client_id),
            client_name=client.client_name,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            state=params.state,
            scopes=params.scopes or [],
            resource=params.resource,
        )
        ticket = new_secret()
        self.store.put(Kind.CONSENT, ticket, pending.model_dump(), expires_at=self._clock() + CONSENT_SECONDS)
        return f'{self.settings.public_base_url}{CONSENT_PATH}?{urlencode({"ticket": ticket})}'

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> GrantedCode | None:
        """Return an unused authorization code that was issued to this client.

        Args:
            client: The MCP client presenting the code.
            authorization_code: The code.

        Returns:
            The code, or None when it is unknown, expired, used or belongs to another client.
        """
        stored = self.store.get(Kind.CODE, authorization_code)
        if not stored or stored['client_id'] != client.client_id:
            return None
        return GrantedCode.model_validate(stored)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: GrantedCode
    ) -> OAuthToken:
        """Turn an authorization code into tokens for the MCP client; a code works once.

        Args:
            client: The MCP client exchanging the code.
            authorization_code: The code, as returned by ``load_authorization_code``.

        Returns:
            An access token and a refresh token.

        Raises:
            TokenError: When the code was already used or the AFAS login is gone.
        """
        if self.store.pop(Kind.CODE, authorization_code.code) is None:
            description = 'authorization code was already used'
            raise TokenError(error='invalid_grant', error_description=description)
        self.remember_client(client)
        return self.issue_tokens(authorization_code.grant_id, authorization_code.client_id, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> GrantedRefreshToken | None:
        """Return a refresh token that was issued to this client.

        Args:
            client: The MCP client presenting the token.
            refresh_token: The token.

        Returns:
            The token, or None when it is unknown, expired, used or belongs to another client.
        """
        stored = self.store.get(Kind.REFRESH, refresh_token)
        if not stored or stored['client_id'] != client.client_id:
            return None
        return GrantedRefreshToken.model_validate(stored)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: GrantedRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Replace the MCP client's tokens; the old access and refresh token stop working.

        Args:
            client: The MCP client refreshing.
            refresh_token: The token, as returned by ``load_refresh_token``.
            scopes: The scopes requested for the new access token.

        Returns:
            A new access token and refresh token.

        Raises:
            TokenError: When the refresh token was already used or the AFAS login is gone.
        """
        if self.store.pop(Kind.REFRESH, refresh_token.token) is None:
            description = 'refresh token was already used'
            raise TokenError(error='invalid_grant', error_description=description)
        self.store.delete_tokens(refresh_token.grant_id)
        self.remember_client(client)
        return self.issue_tokens(refresh_token.grant_id, refresh_token.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> GrantedAccessToken | None:
        """Return the access token an MCP request carries.

        Args:
            token: The bearer token of the request.

        Returns:
            The token, or None when it is unknown, expired or revoked.
        """
        stored = self.store.get(Kind.ACCESS, token)
        return GrantedAccessToken.model_validate(stored) if stored else None

    async def revoke_token(self, token: GrantedAccessToken | GrantedRefreshToken) -> None:
        """Log a user out: forget their AFAS tokens and every token issued for them.

        Args:
            token: Any token of the login to end.
        """
        self.store.delete_grant(token.grant_id)

    async def consent_page(self, request: Request) -> Response:
        """Ask the user whether the named application may act for them, before sending them to AFAS.

        Without this step anyone could register a client and pass a victim a link that silently completes with the
        victim's existing AFAS session.

        Args:
            request: The browser request, carrying the ticket of the pending authorization.

        Returns:
            The consent page, or an error page when the ticket is unknown or expired.
        """
        ticket = request.query_params.get('ticket', '')
        stored = self.store.get(Kind.CONSENT, ticket)
        if stored is None:
            return error_page(self._texts, 'expired')
        pending = PendingAuthorization.model_validate(stored)
        texts = self._texts
        browser = new_secret()
        body = CONSENT_BODY.format(
            title=texts['title'],
            intro=texts['intro'].format(client=html.escape(pending.client_name or texts['unknown_client'])),
            destination=texts['destination'],
            redirect=html.escape(pending.redirect_uri.partition('?')[0]),
            warning=texts['warning'],
            action=CONSENT_PATH,
            ticket=html.escape(ticket),
            browser=browser,
            deny=texts['deny'],
            allow=texts['allow'],
        )
        response = page(texts, texts['title'], body)
        response.set_cookie(
            self._cookie,
            browser,
            max_age=CONSENT_SECONDS,
            path='/',
            secure=self._is_https,
            httponly=True,
            samesite='lax',
        )
        return response

    async def consent_decision(self, request: Request) -> Response:
        """Act on the user's choice: back to the MCP client when denied, on to the AFAS login when allowed.

        The form must carry the value of the cookie the consent page set, so another site cannot submit it for
        the user.

        Args:
            request: The form post of the consent page.

        Returns:
            A redirect, or an error page when the form does not belong to this browser or the ticket is spent.
        """
        form = await request.form()
        browser = request.cookies.get(self._cookie, '')
        if not browser or not secrets.compare_digest(fingerprint(browser), fingerprint(str(form.get('browser', '')))):
            return error_page(self._texts, 'browser')
        stored = self.store.pop(Kind.CONSENT, str(form.get('ticket', '')))
        if stored is None:
            return error_page(self._texts, 'expired')
        pending = PendingAuthorization.model_validate(stored)
        if form.get('decision') != ALLOW:
            return self.back_to_client(pending, error='access_denied', error_description='the user cancelled')
        state = new_secret()
        verifier = new_secret()
        login = PendingLogin(**pending.model_dump(), code_verifier=verifier, browser=fingerprint(browser))
        self.store.put(Kind.LOGIN, state, login.model_dump(), expires_at=self._clock() + CONSENT_SECONDS)
        query = urlencode(
            {
                'response_type': 'code',
                'client_id': str(self.settings.oauth_client_id),
                'redirect_uri': self.settings.oauth_callback_url,
                'state': state,
                'code_challenge': code_challenge(verifier),
                'code_challenge_method': 'S256',
            }
        )
        return RedirectResponse(f'{self.settings.oauth_authorize_url}?{query}', status_code=303, headers=PAGE_HEADERS)

    async def callback(self, request: Request) -> Response:
        """Finish a login when AFAS sends the browser back, and return the user to their MCP client.

        The request must come from the browser that approved the consent page, so a login link passed to someone
        else cannot hand their AFAS session to the sender.

        Args:
            request: The redirect from AFAS, carrying ``state`` and either ``code`` or ``error``.

        Returns:
            A redirect to the MCP client with an authorization code or an error, or an error page when the
            request does not match a login this browser started.
        """
        stored = self.store.pop(Kind.LOGIN, request.query_params.get('state', ''))
        if stored is None:
            return error_page(self._texts, 'expired')
        login = PendingLogin.model_validate(stored)
        browser = request.cookies.get(self._cookie, '')
        if not secrets.compare_digest(fingerprint(browser), login.browser):
            return error_page(self._texts, 'browser')
        code = request.query_params.get('code')
        if not code:
            return self.back_to_client(login, error='access_denied', error_description='AFAS did not log the user in')
        try:
            grant_id, afas_login = await self.logins.start(code, login.code_verifier)
        except (AfasAuthorizationError, httpx2.HTTPError) as error:
            self._logger.warning('AFAS refused to exchange an authorization code: %s', error)
            return self.back_to_client(login, error='server_error', error_description='AFAS login failed')
        self._logger.info('%s logged in through client %s', afas_login.subject, login.client_id)
        return self.back_to_client(login, code=self.issue_code(login, grant_id, afas_login.subject))

    def back_to_client(self, pending: PendingAuthorization, **params: str) -> Response:
        """Redirect the browser to the MCP client's redirect URI and clear the login cookie.

        Args:
            pending: The authorization request being answered.
            **params: The ``code`` or ``error`` parameters of the answer; ``state`` is added.

        Returns:
            The redirect.
        """
        url = construct_redirect_uri(pending.redirect_uri, **params, state=pending.state)
        response = RedirectResponse(url, status_code=303, headers=PAGE_HEADERS)
        response.delete_cookie(self._cookie, path='/', secure=self._is_https, httponly=True, samesite='lax')
        return response

    def remember_client(self, client: OAuthClientInformationFull) -> None:
        """Store an MCP client, or extend its registration when it is in use.

        Args:
            client: The client to keep.
        """
        self.store.put(
            Kind.CLIENT,
            str(client.client_id),
            client.model_dump(mode='json'),
            expires_at=self._clock() + CLIENT_SECONDS,
        )

    def issue_code(self, login: PendingLogin, grant_id: str, subject: str) -> str:
        """Issue the authorization code an MCP client is waiting for after its user logged in.

        Args:
            login: The login attempt the user completed.
            grant_id: The AFAS login it produced.
            subject: The user that logged in.

        Returns:
            The authorization code.
        """
        code = GrantedCode(
            code=new_secret(),
            scopes=login.scopes,
            expires_at=self._clock() + CODE_SECONDS,
            client_id=login.client_id,
            code_challenge=login.code_challenge,
            redirect_uri=AnyUrl(login.redirect_uri),
            redirect_uri_provided_explicitly=login.redirect_uri_provided_explicitly,
            resource=login.resource,
            subject=subject,
            grant_id=grant_id,
        )
        self.store.put(
            Kind.CODE, code.code, code.model_dump(mode='json'), expires_at=code.expires_at, grant_id=grant_id
        )
        return code.code

    def issue_tokens(self, grant_id: str, client_id: str, scopes: list[str]) -> OAuthToken:
        """Create an access token and a refresh token for an MCP client.

        The refresh token lasts as long as the AFAS login behind it, so the client is sent back through the login
        when AFAS would demand one anyway.

        Args:
            grant_id: The AFAS login the tokens act for.
            client_id: The MCP client the tokens are for.
            scopes: The scopes of the tokens.

        Returns:
            The token response for the MCP client.

        Raises:
            TokenError: When the AFAS login no longer exists.
        """
        login = self.logins.load(grant_id)
        if login is None:
            description = 'the AFAS login has ended; log in again'
            raise TokenError(error='invalid_grant', error_description=description)
        now = self._clock()
        expires_at = min(now + ACCESS_TOKEN_SECONDS, login.deadline)
        shared: dict[str, Any] = {'client_id': client_id, 'scopes': scopes, 'subject': login.subject}
        access = GrantedAccessToken(token=new_secret(), expires_at=int(expires_at), grant_id=grant_id, **shared)
        refresh = GrantedRefreshToken(token=new_secret(), expires_at=int(login.deadline), grant_id=grant_id, **shared)
        self.store.put(
            Kind.ACCESS, access.token, access.model_dump(mode='json'), expires_at=expires_at, grant_id=grant_id
        )
        self.store.put(
            Kind.REFRESH, refresh.token, refresh.model_dump(mode='json'), expires_at=login.deadline, grant_id=grant_id
        )
        return OAuthToken(
            access_token=access.token,
            token_type=BEARER,
            expires_in=int(expires_at - now),
            refresh_token=refresh.token,
            scope=' '.join(scopes) if scopes else None,
        )
