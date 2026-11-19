"""Tests for AFAS OAuth 2.0, driven over HTTP against the real server app and a fake AFAS."""

import asyncio
import base64
import json
import re
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthClientInformationFull

from afas_mcp_server import oauth as oauth_module
from afas_mcp_server import server as server_module
from afas_mcp_server.client import AfasAuthorizationError
from afas_mcp_server.oauth import (
    AFAS_LOGIN_SECONDS,
    CLIENT_SECONDS,
    AfasOAuthProvider,
    code_challenge,
    error_code,
    subject_of,
    texts_for,
)
from afas_mcp_server.server import build_server
from tests.conftest import PUBLIC_URL, make_settings

REDIRECT_URI = 'http://localhost:7777/callback'
VERIFIER = 'v' * 64
CLIENT_STATE = 'client-state'
TOKEN_URL = 'https://12345.rest.afas.online/profitrestservices/oauth/token'
NEARLY_AN_HOUR = 3550
MCP_HEADERS = {'Accept': 'application/json, text/event-stream'}


def make_jwt(claims: Any) -> str:  # noqa: ANN401
    """Build an unsigned token in the shape of a JWT, the way AFAS access tokens look."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b'=').decode()
    return f'header.{payload}.signature'


def query_of(response: httpx2.Response) -> dict[str, str]:
    """The query parameters of the URL a redirect points to."""
    return {key: values[0] for key, values in parse_qs(urlsplit(response.headers['location']).query).items()}


class Clock:
    """A clock tests move by hand, started at the real time so the SDK's own expiry checks agree with it."""

    def __init__(self) -> None:
        """Start now."""
        self.now = time.time()

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now


class FakeAfas:
    """A tiny AFAS: a token endpoint that issues numbered tokens for ``jan``, and a version endpoint."""

    def __init__(self) -> None:
        """Start with nothing issued and nothing overridden."""
        self.requests: list[httpx2.Request] = []
        self.issued = 0
        self.token_response: httpx2.Response | None = None
        self.token_failure: Exception | None = None
        self.sends_refresh_tokens = True

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Route token requests to the token endpoint and answer everything else with a version."""
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            return self.token(request)
        return httpx2.Response(200, json={'version': '2026.1'})

    def token(self, request: httpx2.Request) -> httpx2.Response:
        """Issue the next tokens, unless a test asked for a failure; an empty refresh token is always refused."""
        if self.token_failure is not None:
            raise self.token_failure
        if self.token_response is not None:
            return self.token_response
        if parse_qs(request.content.decode()).get('grant_type') == ['refresh_token'] and not self.form(request).get(
            'refresh_token'
        ):
            return httpx2.Response(400, json={'error': 'invalid_request'})
        self.issued += 1
        body: dict[str, Any] = {
            'access_token': self.access_token(self.issued),
            'token_type': 'Bearer',
            'expires_in': 3600,
        }
        if self.sends_refresh_tokens:
            body['refresh_token'] = f'afas-refresh-{self.issued}'
        return httpx2.Response(200, json=body)

    @staticmethod
    def access_token(number: int) -> str:
        """The access token issued by the given token request."""
        return make_jwt({'preferred_username': 'jan@example.test', 'number': number})

    @staticmethod
    def form(request: httpx2.Request) -> dict[str, str]:
        """The form fields of a token request."""
        return {key: values[0] for key, values in parse_qs(request.content.decode(), keep_blank_values=True).items()}

    @property
    def token_forms(self) -> list[dict[str, str]]:
        """The form fields of every token request so far."""
        return [self.form(request) for request in self.requests if str(request.url) == TOKEN_URL]

    @property
    def api_requests(self) -> list[httpx2.Request]:
        """Every request that was not a token request."""
        return [request for request in self.requests if str(request.url) != TOKEN_URL]


class Harness:
    """One running server with a browser-and-MCP-client in one: ``http`` keeps cookies like a browser does."""

    def __init__(self, http: httpx2.AsyncClient, afas: FakeAfas, provider: AfasOAuthProvider, clock: Clock) -> None:
        """Tie the pieces of one test together."""
        self.http = http
        self.afas = afas
        self.provider = provider
        self.clock = clock

    async def register(self, **metadata: Any) -> dict[str, Any]:  # noqa: ANN401
        """Register an MCP client the way clients do it themselves; public unless the metadata says otherwise."""
        body = {
            'redirect_uris': [REDIRECT_URI],
            'client_name': 'Test Client',
            'token_endpoint_auth_method': 'none',
            'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'],
            **metadata,
        }
        response = await self.http.post('/register', json=body)
        assert response.status_code == 201, response.text
        return response.json()

    async def consent(self, client: dict[str, Any]) -> httpx2.Response:
        """Start an authorization request and follow it to the consent page."""
        params = {
            'response_type': 'code',
            'client_id': client['client_id'],
            'redirect_uri': REDIRECT_URI,
            'code_challenge': code_challenge(VERIFIER),
            'code_challenge_method': 'S256',
            'state': CLIENT_STATE,
        }
        response = await self.http.get('/authorize', params=params)
        assert response.status_code == 302, response.text
        assert '/oauth/consent?ticket=' in response.headers['location']
        return await self.http.get(response.headers['location'])

    async def decide(self, page: httpx2.Response, decision: str = 'allow', **overrides: str) -> httpx2.Response:
        """Press a button on the consent page."""
        form = {
            'ticket': field(page, 'ticket'),
            'browser': field(page, 'browser'),
            'decision': decision,
            **overrides,
        }
        return await self.http.post('/oauth/consent', data=form)

    async def at_afas(self, client: dict[str, Any]) -> httpx2.Response:
        """Approve the consent page; the result is the redirect to the AFAS login."""
        response = await self.decide(await self.consent(client))
        assert response.status_code == 303, response.text
        return response

    async def code(self, client: dict[str, Any]) -> str:
        """Log in at the fake AFAS and return the authorization code the MCP client receives."""
        state = query_of(await self.at_afas(client))['state']
        response = await self.http.get('/oauth/afas/callback', params={'code': 'afas-code', 'state': state})
        assert response.status_code == 303, response.text
        assert response.headers['location'].startswith(REDIRECT_URI)
        answer = query_of(response)
        assert answer['state'] == CLIENT_STATE
        return answer['code']

    async def token(self, client: dict[str, Any], **grant: str) -> httpx2.Response:
        """Call the token endpoint as the MCP client."""
        form = {'client_id': client['client_id'], **grant}
        if client.get('client_secret'):
            form['client_secret'] = client['client_secret']
        return await self.http.post('/token', data=form)

    async def login(self, client: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run the whole flow and return the MCP client's tokens."""
        client = client or await self.register()
        response = await self.token(
            client,
            grant_type='authorization_code',
            code=await self.code(client),
            code_verifier=VERIFIER,
            redirect_uri=REDIRECT_URI,
        )
        assert response.status_code == 200, response.text
        return {**response.json(), 'client': client}

    async def call(self, access_token: str | None, tool: str = 'afas_connection_info') -> httpx2.Response:
        """Call a tool over the MCP endpoint with a bearer token."""
        headers = dict(MCP_HEADERS)
        if access_token is not None:
            headers['Authorization'] = f'Bearer {access_token}'
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': tool, 'arguments': {}}}
        return await self.http.post('/mcp', json=body, headers=headers)

    async def client_record(self, client: dict[str, Any]) -> OAuthClientInformationFull:
        """The stored registration of a client."""
        record = await self.provider.get_client(client['client_id'])
        assert record is not None
        return record


def field(page: httpx2.Response, name: str) -> str:
    """The value of a hidden form field on the consent page."""
    assert page.status_code == 200, page.text
    match = re.search(rf'name="{name}" value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


@asynccontextmanager
async def running(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> AsyncGenerator[Harness]:  # noqa: ANN401
    """Run the server app in-process for AFAS OAuth 2.0, on a fake clock and against a fake AFAS."""
    afas = FakeAfas()
    clock = Clock()
    providers: list[AfasOAuthProvider] = []

    def provider_on_fake_clock(settings: Any, transport: Any = None) -> AfasOAuthProvider:  # noqa: ANN401
        providers.append(AfasOAuthProvider(settings, transport, clock))
        return providers[0]

    monkeypatch.setattr(server_module, 'AfasOAuthProvider', provider_on_fake_clock)
    settings = make_settings(**overrides)
    server = build_server(settings, transport=httpx2.MockTransport(afas))
    app = server.streamable_http_app(stateless_http=True, json_response=True, host='0.0.0.0')  # noqa: S104
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url=settings.public_base_url) as http,
    ):
        yield Harness(http, afas, providers[0], clock)
    await providers[0].aclose()


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Harness]:
    """A running server with the default settings: https, Dutch pages."""
    # pytest runs this generator to its end after the test, which is what exits the context.
    async with running(monkeypatch) as started:
        yield started


def test_code_challenge_matches_the_rfc_7636_example() -> None:
    """The S256 challenge is the URL-safe, unpadded SHA-256 of the verifier."""
    verifier = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'
    assert code_challenge(verifier) == 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM'


@pytest.mark.parametrize(
    ('access_token', 'expected'),
    [
        (make_jwt({'sub': 'u-17', 'preferred_username': 'jan@example.test'}), 'jan@example.test'),
        (make_jwt({'sub': 'u-17'}), 'u-17'),
        (make_jwt({'scope': 'profit'}), 'unknown-user'),
        (make_jwt(['not', 'claims']), 'unknown-user'),
        ('header.!!!.signature', 'unknown-user'),
        ('an-opaque-token', 'unknown-user'),
    ],
)
def test_subject_of_reads_the_user_from_a_jwt(access_token: str, expected: str) -> None:
    """The audit label comes from the token's claims when it has any, and never fails on a token without."""
    assert subject_of(access_token) == expected


@pytest.mark.parametrize(
    ('response', 'expected'),
    [
        (httpx2.Response(400, json={'error': 'invalid_grant', 'error_description': 'secret detail'}), 'invalid_grant'),
        (httpx2.Response(400, json={'message': 'nope'}), 'no error code'),
        (httpx2.Response(400, json=['nope']), 'no error code'),
        (httpx2.Response(502, text='<html>Bad gateway</html>'), 'no error code'),
    ],
)
def test_error_code_reports_only_the_oauth_error(response: httpx2.Response, expected: str) -> None:
    """Logs get the error code of a refused token request, not whatever else the body holds."""
    assert error_code(response) == expected


def test_page_texts_follow_the_afas_language() -> None:
    """Dutch environments get Dutch pages; every other language falls back to English."""
    assert texts_for('nl-NL')['lang'] == 'nl'
    assert texts_for('en-us')['lang'] == 'en'
    assert texts_for('fr-fr')['lang'] == 'en'


@pytest.mark.anyio
async def test_server_advertises_itself_as_authorization_server(harness: Harness) -> None:
    """MCP clients discover where to register and log in from the well-known documents."""
    metadata = (await harness.http.get('/.well-known/oauth-authorization-server')).json()
    assert metadata['issuer'] == PUBLIC_URL
    assert metadata['authorization_endpoint'] == f'{PUBLIC_URL}/authorize'
    assert metadata['registration_endpoint'] == f'{PUBLIC_URL}/register'
    assert metadata['code_challenge_methods_supported'] == ['S256']
    resource = (await harness.http.get('/.well-known/oauth-protected-resource/mcp')).json()
    assert resource['resource'] == f'{PUBLIC_URL}/mcp'
    assert resource['authorization_servers'] == [PUBLIC_URL]


@pytest.mark.anyio
async def test_health_endpoint_needs_no_login(harness: Harness) -> None:
    """The container health check can reach the server without a token."""
    response = await harness.http.get('/healthz')
    assert (response.status_code, response.json()) == (200, {'status': 'ok'})


@pytest.mark.anyio
async def test_mcp_endpoint_requires_a_login(harness: Harness) -> None:
    """Without a token, or with a made-up one, no tool can be called, and the answer says where to log in."""
    for token in (None, 'made-up'):
        response = await harness.call(token)
        assert response.status_code == 401
        assert 'resource_metadata=' in response.headers['www-authenticate']
    assert harness.afas.requests == []


@pytest.mark.anyio
async def test_login_then_tools_call_afas_as_that_user(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    """The whole flow: register, consent, AFAS login, token, and a tool call made with the user's own AFAS token."""
    client = await harness.register()
    redirect = await harness.at_afas(client)
    assert redirect.headers['location'].startswith('https://12345.rest.afas.online/profitrestservices/oauth/authorize?')
    afas_request = query_of(redirect)
    assert afas_request['response_type'] == 'code'
    assert afas_request['client_id'] == 'connector-id'
    assert afas_request['redirect_uri'] == f'{PUBLIC_URL}/oauth/afas/callback'
    assert afas_request['code_challenge_method'] == 'S256'

    callback = await harness.http.get(
        '/oauth/afas/callback', params={'code': 'afas-code', 'state': afas_request['state']}
    )
    assert 'Max-Age=0' in callback.headers['set-cookie']
    exchange = harness.afas.token_forms[0]
    assert exchange['grant_type'] == 'authorization_code'
    assert exchange['code'] == 'afas-code'
    assert exchange['client_id'] == 'connector-id'
    assert exchange['client_secret'] == 'connector-secret'
    assert exchange['redirect_uri'] == f'{PUBLIC_URL}/oauth/afas/callback'
    assert code_challenge(exchange['code_verifier']) == afas_request['code_challenge']

    tokens = await harness.token(
        client,
        grant_type='authorization_code',
        code=query_of(callback)['code'],
        code_verifier=VERIFIER,
        redirect_uri=REDIRECT_URI,
    )
    assert tokens.status_code == 200
    assert tokens.json()['token_type'] == 'Bearer'
    assert tokens.json()['expires_in'] == 3600
    assert 'afas' not in tokens.text.lower()

    with caplog.at_level('INFO', logger='afas_mcp_server.audit'):
        response = await harness.call(tokens.json()['access_token'])
    result = response.json()['result']
    assert result['isError'] is False
    assert result['structuredContent']['authentication'] == 'afas-oauth2'
    assert harness.afas.api_requests[0].headers['Authorization'] == f'Bearer {FakeAfas.access_token(1)}'
    assert 'GET profitversion as jan@example.test' in caplog.messages


@pytest.mark.anyio
async def test_consent_page_names_the_client_without_trusting_it(harness: Harness) -> None:
    """The page shows who is asking and where the user returns to, escaped, unframeable and uncached."""
    client = await harness.register(client_name='<script>alert(1)</script>')
    page = await harness.consent(client)
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page.text
    assert '<script>' not in page.text
    assert f'<code>{REDIRECT_URI}</code>' in page.text
    assert 'Toegang tot AFAS geven?' in page.text
    assert page.headers['x-frame-options'] == 'DENY'
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']
    assert page.headers['cache-control'] == 'no-store'
    cookie = page.headers['set-cookie']
    assert cookie.startswith('__Host-afas-mcp-login=')
    assert all(flag in cookie for flag in ('HttpOnly', 'Secure', 'SameSite=lax', 'Path=/'))


@pytest.mark.anyio
async def test_unnamed_client_is_called_unknown_in_english(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client that registered no name is not given one, and non-Dutch environments get English pages."""
    async with running(monkeypatch, language='en-us') as harness:
        page = await harness.consent(await harness.register(client_name=None))
        assert '<strong>An unknown application</strong>' in page.text
        assert '<html lang="en">' in page.text


@pytest.mark.anyio
async def test_plain_http_on_this_machine_uses_a_plain_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browsers refuse `__Host-` cookies over http, so a local test setup gets an ordinary one and still works."""
    async with running(monkeypatch, public_url='http://localhost:8000') as harness:
        client = await harness.register()
        page = await harness.consent(client)
        assert page.headers['set-cookie'].startswith('afas-mcp-login=')
        assert 'Secure' not in page.headers['set-cookie']
        assert (await harness.decide(page)).status_code == 303


@pytest.mark.anyio
async def test_unknown_consent_ticket_shows_an_error(harness: Harness) -> None:
    """A made-up or expired ticket gets an explanation instead of a consent form."""
    for path in ('/oauth/consent', '/oauth/consent?ticket=made-up'):
        page = await harness.http.get(path)
        assert page.status_code == 400
        assert 'Inloggen is niet gelukt' in page.text
        assert '<form' not in page.text


@pytest.mark.anyio
async def test_consent_ticket_expires(harness: Harness) -> None:
    """A consent page left open for more than ten minutes can no longer be approved."""
    page = await harness.consent(await harness.register())
    harness.clock.now += 601
    assert (await harness.decide(page)).status_code == 400


@pytest.mark.anyio
async def test_consent_cannot_be_submitted_for_someone_else(harness: Harness) -> None:
    """The form only counts with the cookie of the browser that was shown the page: no cross-site approval."""
    page = await harness.consent(await harness.register())
    forged = await harness.decide(page, browser='guessed')
    assert forged.status_code == 400
    assert 'andere browser' in forged.text
    harness.http.cookies.clear()
    assert (await harness.decide(page)).status_code == 400
    assert harness.afas.requests == []


@pytest.mark.anyio
async def test_consent_ticket_works_once(harness: Harness) -> None:
    """Approving twice does not start two logins."""
    page = await harness.consent(await harness.register())
    assert (await harness.decide(page)).status_code == 303
    again = await harness.decide(page)
    assert again.status_code == 400
    assert 'verlopen of al gebruikt' in again.text


@pytest.mark.anyio
async def test_cancelling_returns_to_the_client_with_an_error(harness: Harness) -> None:
    """Cancel sends the browser back to the MCP client, which learns the user said no."""
    response = await harness.decide(await harness.consent(await harness.register()), decision='deny')
    assert response.status_code == 303
    assert response.headers['location'].startswith(REDIRECT_URI)
    assert query_of(response) == {
        'error': 'access_denied',
        'error_description': 'the user cancelled',
        'state': CLIENT_STATE,
    }


@pytest.mark.anyio
async def test_callback_with_unknown_state_shows_an_error(harness: Harness) -> None:
    """A callback that matches no login attempt is not exchanged at AFAS."""
    response = await harness.http.get('/oauth/afas/callback', params={'code': 'afas-code', 'state': 'made-up'})
    assert response.status_code == 400
    assert harness.afas.requests == []


@pytest.mark.anyio
async def test_login_link_does_not_work_in_another_browser(harness: Harness) -> None:
    """Passing someone the AFAS link of your own login attempt does not hand you their AFAS session."""
    state = query_of(await harness.at_afas(await harness.register()))['state']
    harness.http.cookies.clear()
    response = await harness.http.get('/oauth/afas/callback', params={'code': 'victims-code', 'state': state})
    assert response.status_code == 400
    assert 'andere browser' in response.text
    assert harness.afas.requests == []


@pytest.mark.anyio
async def test_afas_login_failure_returns_to_the_client(harness: Harness) -> None:
    """When AFAS sends the user back without a code, the MCP client gets an error instead of a code."""
    state = query_of(await harness.at_afas(await harness.register()))['state']
    response = await harness.http.get('/oauth/afas/callback', params={'error': 'access_denied', 'state': state})
    assert query_of(response)['error'] == 'access_denied'
    assert query_of(response)['state'] == CLIENT_STATE
    assert harness.afas.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    'failure',
    [
        httpx2.Response(400, json={'error': 'invalid_grant'}),
        httpx2.Response(200, json={'unexpected': 'body'}),
        httpx2.ConnectError('name resolution failed'),
    ],
)
async def test_failed_code_exchange_returns_to_the_client(
    harness: Harness, failure: httpx2.Response | Exception
) -> None:
    """A refused code, a useless answer or an unreachable AFAS all end as a server error at the MCP client."""
    state = query_of(await harness.at_afas(await harness.register()))['state']
    if isinstance(failure, Exception):
        harness.afas.token_failure = failure
    else:
        harness.afas.token_response = failure
    response = await harness.http.get('/oauth/afas/callback', params={'code': 'afas-code', 'state': state})
    assert response.status_code == 303
    assert query_of(response) == {
        'error': 'server_error',
        'error_description': 'AFAS login failed',
        'state': CLIENT_STATE,
    }


@pytest.mark.anyio
async def test_authorization_code_works_once_and_only_for_its_client(harness: Harness) -> None:
    """A code is spent by its first exchange and is invisible to every other client."""
    client = await harness.register()
    other = await harness.client_record(await harness.register())
    code = await harness.code(client)
    assert await harness.provider.load_authorization_code(other, code) is None
    record = await harness.client_record(client)
    loaded = await harness.provider.load_authorization_code(record, code)
    assert loaded is not None
    assert loaded.subject == 'jan@example.test'
    await harness.provider.exchange_authorization_code(record, loaded)
    with pytest.raises(TokenError) as refusal:
        await harness.provider.exchange_authorization_code(record, loaded)
    assert refusal.value.error_description == 'authorization code was already used'
    grant = {'grant_type': 'authorization_code', 'code': code, 'code_verifier': VERIFIER, 'redirect_uri': REDIRECT_URI}
    refused = await harness.token(client, **grant)
    assert (refused.status_code, refused.json()['error']) == (400, 'invalid_grant')


@pytest.mark.anyio
async def test_refreshing_replaces_both_mcp_tokens(harness: Harness) -> None:
    """After a refresh the old access token and the old refresh token are dead, and the new ones work."""
    tokens = await harness.login()
    refreshed = await harness.token(tokens['client'], grant_type='refresh_token', refresh_token=tokens['refresh_token'])
    assert refreshed.status_code == 200
    assert (await harness.call(tokens['access_token'])).status_code == 401
    assert (await harness.call(refreshed.json()['access_token'])).status_code == 200
    replayed = await harness.token(tokens['client'], grant_type='refresh_token', refresh_token=tokens['refresh_token'])
    assert (replayed.status_code, replayed.json()['error']) == (400, 'invalid_grant')
    assert len(harness.afas.token_forms) == 1


@pytest.mark.anyio
async def test_refresh_token_works_once_and_only_for_its_client(harness: Harness) -> None:
    """A refresh token is spent by its first exchange and is invisible to every other client."""
    tokens = await harness.login()
    other = await harness.client_record(await harness.register())
    assert await harness.provider.load_refresh_token(other, tokens['refresh_token']) is None
    record = await harness.client_record(tokens['client'])
    loaded = await harness.provider.load_refresh_token(record, tokens['refresh_token'])
    assert loaded is not None
    renewed = await harness.provider.exchange_refresh_token(record, loaded, ['profit'])
    assert renewed.scope == 'profit'
    with pytest.raises(TokenError) as refusal:
        await harness.provider.exchange_refresh_token(record, loaded, [])
    assert refusal.value.error_description == 'refresh token was already used'


@pytest.mark.anyio
async def test_mcp_access_token_expires_after_an_hour(harness: Harness) -> None:
    """The MCP client's access token is short-lived; it has to come back with its refresh token."""
    tokens = await harness.login()
    harness.clock.now += 3601
    assert await harness.provider.load_access_token(tokens['access_token']) is None
    assert await harness.provider.load_refresh_token(
        await harness.client_record(tokens['client']), tokens['refresh_token']
    )


@pytest.mark.anyio
async def test_mcp_tokens_never_outlive_the_afas_login(harness: Harness) -> None:
    """AFAS demands a new login after 30 days, so tokens issued near that moment expire with it."""
    tokens = await harness.login()
    harness.clock.now += AFAS_LOGIN_SECONDS - 100
    record = await harness.client_record(tokens['client'])
    loaded = await harness.provider.load_refresh_token(record, tokens['refresh_token'])
    assert loaded is not None
    renewed = await harness.provider.exchange_refresh_token(record, loaded, [])
    assert renewed.expires_in == 100
    harness.clock.now += 100
    assert await harness.provider.load_refresh_token(record, str(renewed.refresh_token)) is None
    with pytest.raises(TokenError) as refusal:
        harness.provider.issue_tokens(loaded.grant_id, loaded.client_id, [])
    assert refusal.value.error_description == 'the AFAS login has ended; log in again'


@pytest.mark.anyio
async def test_revoking_a_token_logs_the_user_out(harness: Harness) -> None:
    """Revocation forgets the AFAS tokens and everything issued for that login."""
    client = await harness.register(token_endpoint_auth_method='client_secret_post')
    tokens = await harness.login(client)
    access = await harness.provider.load_access_token(tokens['access_token'])
    assert access is not None
    assert harness.provider.logins.load(access.grant_id) is not None
    form = {
        'token': tokens['refresh_token'],
        'client_id': client['client_id'],
        'client_secret': client['client_secret'],
    }
    assert (await harness.http.post('/revoke', data=form)).status_code == 200
    assert harness.provider.logins.load(access.grant_id) is None
    assert (await harness.call(tokens['access_token'])).status_code == 401


@pytest.mark.anyio
async def test_client_registrations_are_capped(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """Registration is open to anyone who can reach the server, so the store refuses to grow without bound."""
    monkeypatch.setattr(oauth_module, 'MAX_CLIENTS', 1)
    await harness.register()
    refused = await harness.http.post('/register', json={'redirect_uris': [REDIRECT_URI]})
    assert refused.status_code == 400
    assert 'too many registered clients' in refused.text


@pytest.mark.anyio
async def test_unused_client_registrations_lapse(harness: Harness) -> None:
    """A client nobody logs in with is forgotten after ninety days; one in use is kept."""
    idle = await harness.register()
    tokens = await harness.login()
    harness.clock.now += CLIENT_SECONDS - 60
    harness.provider.remember_client(await harness.client_record(tokens['client']))
    harness.clock.now += 120
    assert await harness.provider.get_client(idle['client_id']) is None
    assert await harness.provider.get_client(tokens['client']['client_id']) is not None


@pytest.mark.anyio
async def test_afas_token_is_refreshed_shortly_before_it_expires(harness: Harness) -> None:
    """Tool calls keep working past the hour an AFAS access token lasts, with one refresh for concurrent calls."""
    tokens = await harness.login()
    await harness.call(tokens['access_token'])
    assert len(harness.afas.token_forms) == 1
    harness.clock.now += NEARLY_AN_HOUR
    responses = await asyncio.gather(harness.call(tokens['access_token']), harness.call(tokens['access_token']))
    assert [response.json()['result']['isError'] for response in responses] == [False, False]
    assert harness.afas.token_forms[1:] == [
        {
            'grant_type': 'refresh_token',
            'refresh_token': 'afas-refresh-1',
            'client_id': 'connector-id',
            'client_secret': 'connector-secret',
        }
    ]
    assert harness.afas.api_requests[-1].headers['Authorization'] == f'Bearer {FakeAfas.access_token(2)}'


@pytest.mark.anyio
async def test_afas_refresh_token_is_kept_when_afas_sends_no_new_one(harness: Harness) -> None:
    """AFAS rotates refresh tokens; should it ever not, the one in hand stays in use."""
    tokens = await harness.login()
    harness.afas.sends_refresh_tokens = False
    harness.clock.now += NEARLY_AN_HOUR
    await harness.call(tokens['access_token'])
    harness.clock.now += NEARLY_AN_HOUR
    refreshed = await harness.token(tokens['client'], grant_type='refresh_token', refresh_token=tokens['refresh_token'])
    await harness.call(refreshed.json()['access_token'])
    assert [form.get('refresh_token') for form in harness.afas.token_forms[1:]] == ['afas-refresh-1', 'afas-refresh-1']


@pytest.mark.anyio
@pytest.mark.parametrize('sends_refresh_tokens', [True, False])
async def test_refused_afas_refresh_ends_the_login(harness: Harness, *, sends_refresh_tokens: bool) -> None:
    """When AFAS will not refresh (revoked, blocked, or no refresh token at all) the user has to log in again."""
    harness.afas.sends_refresh_tokens = sends_refresh_tokens
    tokens = await harness.login()
    if sends_refresh_tokens:
        harness.afas.token_response = httpx2.Response(400, json={'error': 'invalid_grant'})
    harness.clock.now += NEARLY_AN_HOUR
    result = (await harness.call(tokens['access_token'])).json()['result']
    assert result['isError'] is True
    assert 'your AFAS login has expired; reconnect to log in again' in result['content'][0]['text']
    assert harness.afas.api_requests == []
    assert (await harness.call(tokens['access_token'])).status_code == 401


@pytest.mark.anyio
async def test_afas_calls_outside_a_logged_in_request_are_refused(harness: Harness) -> None:
    """Without the token of an MCP request there is nobody to call AFAS as."""
    with pytest.raises(AfasAuthorizationError, match='not logged in'):
        await harness.provider.logins.authorize_call()
    with pytest.raises(AfasAuthorizationError, match='has ended'):
        await harness.provider.logins.fresh('no-such-login')
