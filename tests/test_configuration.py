"""Tests for the settings."""

import pytest
from pydantic import ValidationError

from afas_mcp_server.configuration import Environment, Settings
from tests.conftest import CLIENT_SECRET, ENCRYPTION_KEY, PUBLIC_URL, make_settings


@pytest.mark.parametrize(
    ('environment', 'expected'),
    [
        (Environment.PRODUCTION, 'https://12345.rest.afas.online/profitrestservices'),
        (Environment.TEST, 'https://12345.resttest.afas.online/profitrestservices'),
        (Environment.ACCEPT, 'https://12345.restaccept.afas.online/profitrestservices'),
    ],
)
def test_rest_base_url_per_environment(environment: Environment, expected: str) -> None:
    """The environment kind selects the AFAS Online hostname."""
    assert make_settings(environment=environment).rest_base_url == expected


def test_environment_accepts_its_string_value() -> None:
    """Environment variables arrive as strings and are mapped onto the enum."""
    assert make_settings(environment='test').environment is Environment.TEST


def test_base_url_override_wins_and_loses_trailing_slash() -> None:
    """An explicit base URL replaces the derived one, without a trailing slash."""
    settings = make_settings(member_id=None, base_url='https://afas.example.test/ProfitRestServices/')
    assert settings.rest_base_url == 'https://afas.example.test/ProfitRestServices'


def test_member_id_or_base_url_is_required() -> None:
    """Without either there is no endpoint to call."""
    with pytest.raises(ValidationError, match='AFAS_MEMBER_ID'):
        make_settings(member_id='  ')


@pytest.mark.parametrize('member_id', ['evil.example/a?', '12345.evil', '\uff11\uff12\uff13'])
def test_member_id_must_be_a_number(member_id: str) -> None:
    """The member id becomes part of the hostname, so anything but digits could send users and tokens elsewhere."""
    with pytest.raises(ValidationError, match='AFAS_MEMBER_ID must be a number'):
        make_settings(member_id=member_id)


@pytest.mark.parametrize('base_url', ['http://proxy.example/profitrestservices', 'ftp://proxy.example', 'proxy'])
def test_base_url_must_be_https(base_url: str) -> None:
    """A plain-http base URL would send access tokens over the network unencrypted."""
    with pytest.raises(ValidationError, match='AFAS_BASE_URL must be an https URL'):
        make_settings(base_url=base_url)


def test_base_url_may_be_plain_http_on_this_machine() -> None:
    """A local proxy or test double on the loopback interface is reachable without TLS."""
    assert make_settings(base_url='http://localhost:8080/afas/').rest_base_url == 'http://localhost:8080/afas'


def test_oauth_urls_derive_from_the_base_url_and_the_public_url() -> None:
    """The AFAS OAuth endpoints hang off the REST base URL; the callback off the public URL."""
    settings = make_settings(public_url=f'{PUBLIC_URL}/')
    assert settings.oauth_authorize_url == 'https://12345.rest.afas.online/profitrestservices/oauth/authorize'
    assert settings.oauth_token_url == 'https://12345.rest.afas.online/profitrestservices/oauth/token'
    assert settings.public_base_url == PUBLIC_URL
    assert settings.oauth_callback_url == f'{PUBLIC_URL}/oauth/afas/callback'


@pytest.mark.parametrize('missing', ['oauth_client_id', 'oauth_client_secret', 'public_url', 'encryption_key'])
def test_every_oauth_setting_is_required(missing: str) -> None:
    """Without the app connector, the public URL or the key of the token store nobody can log in."""
    with pytest.raises(ValidationError, match=missing):
        make_settings(**{missing: None})


@pytest.mark.parametrize(
    ('overrides', 'expected'),
    [
        ({'oauth_client_id': ''}, 'oauth_client_id'),
        ({'oauth_client_secret': ''}, 'oauth_client_secret'),
        ({'public_url': 'http://mcp.example.test'}, 'AFAS_PUBLIC_URL'),
        ({'public_url': 'mcp.example.test'}, 'AFAS_PUBLIC_URL'),
        ({'encryption_key': 'too-short'}, 'AFAS_ENCRYPTION_KEY'),
    ],
)
def test_unusable_oauth_settings_are_rejected(overrides: dict[str, str], expected: str) -> None:
    """The connector credentials may not be empty, the public URL must be https and the key a Fernet key."""
    with pytest.raises(ValidationError, match=expected):
        make_settings(**overrides)


def test_public_url_may_be_plain_http_on_this_machine() -> None:
    """A developer can try the login flow against a server on the loopback interface."""
    assert make_settings(public_url='http://localhost:8000').oauth_callback_url.startswith('http://localhost:8000/')


def test_log_level_is_case_insensitive() -> None:
    """Lower-case levels are accepted and normalised."""
    assert make_settings(log_level='debug').python_log_level == 'DEBUG'


def test_unknown_log_level_is_rejected() -> None:
    """A typo in the level fails fast instead of at the first log call."""
    with pytest.raises(ValidationError, match='AFAS_LOG_LEVEL'):
        make_settings(log_level='loud')


@pytest.mark.usefixtures('oauth_environment')
def test_settings_read_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every setting is read from an AFAS_ prefixed environment variable."""
    monkeypatch.setenv('AFAS_MEMBER_ID', '99999')
    monkeypatch.setenv('AFAS_ENVIRONMENT', 'accept')
    monkeypatch.setenv('AFAS_ALLOW_WRITES', 'true')
    monkeypatch.setenv('AFAS_MAX_TAKE', '250')
    settings = Settings(_env_file=None)
    assert settings.rest_base_url == 'https://99999.restaccept.afas.online/profitrestservices'
    assert settings.allow_writes is True
    assert settings.max_take == 250
    assert settings.oauth_client_secret.get_secret_value() == CLIENT_SECRET


def test_secrets_do_not_leak_in_repr() -> None:
    """The client secret and the encryption key must not show up when settings are printed or logged."""
    printed = repr(make_settings())
    assert CLIENT_SECRET not in printed
    assert ENCRYPTION_KEY not in printed
