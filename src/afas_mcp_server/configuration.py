"""Runtime configuration for the AFAS MCP server, read from ``AFAS_*`` environment variables."""

from enum import StrEnum
from typing import Annotated, Self
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AFAS_ONLINE_DOMAIN = 'afas.online'
REST_SERVICES_PATH = 'profitrestservices'
LOG_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1')
OAUTH_AUTHORIZE_PATH = 'oauth/authorize'
OAUTH_TOKEN_PATH = 'oauth/token'  # noqa: S105  # a URL path, not a credential
OAUTH_CALLBACK_PATH = '/oauth/afas/callback'
DEFAULT_DATABASE_PATH = 'afas-mcp-server.sqlite3'


class Environment(StrEnum):
    """The kind of AFAS Online environment, which selects the REST hostname."""

    PRODUCTION = 'production'
    TEST = 'test'
    ACCEPT = 'accept'

    @property
    def host_prefix(self) -> str:
        """The hostname fragment AFAS Online uses for this environment kind."""
        return HOST_PREFIXES[self]


HOST_PREFIXES = {
    Environment.PRODUCTION: 'rest',
    Environment.TEST: 'resttest',
    Environment.ACCEPT: 'restaccept',
}


def is_secure_url(url: str) -> bool:
    """Return whether ``url`` is an ``https`` URL, or a plain ``http`` URL on this machine.

    Args:
        url: The URL to inspect.

    Returns:
        True when credentials sent to the URL cannot be read on the network.
    """
    parts = urlsplit(url)
    if not parts.hostname:
        return False
    return parts.scheme == 'https' or (parts.scheme == 'http' and parts.hostname in LOOPBACK_HOSTS)


def is_encryption_key(candidate: str) -> bool:
    """Return whether ``candidate`` is a usable Fernet key: 32 random bytes, URL-safe base64 encoded.

    Args:
        candidate: The text to inspect.

    Returns:
        True when the key can encrypt the token store.
    """
    try:
        Fernet(candidate.encode('ascii'))
    except ValueError:
        return False
    return True


class Settings(BaseSettings):
    """Connection and behaviour settings, populated from ``AFAS_*`` environment variables or a ``.env`` file.

    Either ``member_id`` (optionally with ``environment``) or an explicit ``base_url`` must be provided, together
    with the OAuth app connector users log in through, the public URL of this server and the key of its token store.
    """

    model_config = SettingsConfigDict(env_prefix='AFAS_', env_file='.env', env_file_encoding='utf-8', extra='ignore')

    oauth_client_id: Annotated[
        str,
        Field(min_length=1, description='Client id of the AFAS app connector with authentication type OAuth.'),
    ]
    oauth_client_secret: Annotated[
        SecretStr, Field(min_length=1, description='Client secret of the OAuth app connector.')
    ]
    public_url: Annotated[
        str,
        Field(description='Public https URL of this server; AFAS redirects users back to it after login.'),
    ]
    encryption_key: Annotated[
        SecretStr,
        Field(description='Fernet key that encrypts the stored AFAS tokens of logged-in users.'),
    ]
    database_path: Annotated[
        str, Field(description='SQLite file that holds registered MCP clients and user logins.')
    ] = DEFAULT_DATABASE_PATH
    member_id: Annotated[str | None, Field(description='AFAS Online environment number, e.g. 12345.')] = None
    environment: Annotated[Environment, Field(description='production, test or accept.')] = Environment.PRODUCTION
    base_url: Annotated[
        str | None,
        Field(description='Full REST base URL; overrides member_id and environment.'),
    ] = None
    allow_writes: Annotated[bool, Field(description='Register the insert, update and delete tools.')] = False
    timeout_seconds: Annotated[float, Field(gt=0, description='HTTP timeout per AFAS call, in seconds.')] = 60.0
    default_take: Annotated[int, Field(ge=1, description='Rows per page when a tool call gives no take.')] = 100
    max_take: Annotated[int, Field(ge=1, description='Largest page size a tool call may request.')] = 1000
    language: Annotated[
        str,
        Field(description='Accept-Language for AFAS messages: nl-nl, nl-be, fr-fr, de-de or en-us.'),
    ] = 'nl-nl'
    log_level: Annotated[str, Field(description='Python log level for the server process.')] = 'INFO'

    @model_validator(mode='after')
    def check_consistency(self) -> Self:
        """Reject settings that cannot produce a safe base URL, a working login or a usable log level.

        Returns:
            The validated settings, unchanged.

        Raises:
            ValueError: Naming the first setting that is missing or unusable.
        """
        problem = self.endpoint_problem or self.login_problem or self.log_level_problem
        if problem:
            raise ValueError(problem)
        return self

    @property
    def endpoint_problem(self) -> str | None:
        """What is wrong with the settings that locate AFAS, or None when they are fine."""
        if self.base_url:
            return None if is_secure_url(self.base_url) else 'AFAS_BASE_URL must be an https URL'
        member = (self.member_id or '').strip()
        if not member:
            return 'set AFAS_MEMBER_ID (optionally with AFAS_ENVIRONMENT) or AFAS_BASE_URL'
        return None if member.isascii() and member.isdigit() else 'AFAS_MEMBER_ID must be a number, e.g. 12345'

    @property
    def login_problem(self) -> str | None:
        """What is wrong with the settings users log in with, or None when they are fine."""
        if not is_secure_url(self.public_url):
            return 'AFAS_PUBLIC_URL must be the https URL users reach this server on'
        if not is_encryption_key(self.encryption_key.get_secret_value()):
            return 'AFAS_ENCRYPTION_KEY must be a Fernet key (32 bytes, URL-safe base64)'
        return None

    @property
    def log_level_problem(self) -> str | None:
        """What is wrong with the log level, or None when it is fine."""
        if self.python_log_level in LOG_LEVELS:
            return None
        return f'AFAS_LOG_LEVEL must be one of {", ".join(LOG_LEVELS)}, not {self.log_level!r}'

    @property
    def python_log_level(self) -> str:
        """The log level as Python's ``logging`` module spells it."""
        return self.log_level.strip().upper()

    @property
    def rest_base_url(self) -> str:
        """The REST services base URL, without a trailing slash."""
        if self.base_url:
            return self.base_url.rstrip('/')
        member = (self.member_id or '').strip()
        return f'https://{member}.{self.environment.host_prefix}.{AFAS_ONLINE_DOMAIN}/{REST_SERVICES_PATH}'

    @property
    def oauth_authorize_url(self) -> str:
        """The AFAS endpoint a user's browser is sent to for logging in."""
        return f'{self.rest_base_url}/{OAUTH_AUTHORIZE_PATH}'

    @property
    def oauth_token_url(self) -> str:
        """The AFAS endpoint that exchanges authorization codes and refresh tokens."""
        return f'{self.rest_base_url}/{OAUTH_TOKEN_PATH}'

    @property
    def public_base_url(self) -> str:
        """The public URL of this server, without a trailing slash."""
        return self.public_url.rstrip('/')

    @property
    def oauth_callback_url(self) -> str:
        """The redirect URL to register on the AFAS app connector."""
        return f'{self.public_base_url}{OAUTH_CALLBACK_PATH}'
