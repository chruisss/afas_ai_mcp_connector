"""Shared test fixtures for afas_mcp_server."""

import base64
import os
from pathlib import Path
from typing import Any

import pytest

from afas_mcp_server.client import Authorization
from afas_mcp_server.configuration import Settings

ENCRYPTION_KEY = base64.urlsafe_b64encode(b'k' * 32).decode('ascii')
OTHER_ENCRYPTION_KEY = base64.urlsafe_b64encode(b'o' * 32).decode('ascii')
PUBLIC_URL = 'https://mcp.example.test'
CLIENT_SECRET = 'connector-secret'
JAN = 'jan@example.test'
JANS_HEADER = 'Bearer jans-afas-token'

UPDATE_SCHEMA: dict[str, Any] = {
    'id': 'KnSubject',
    'description': 'Dossieritem',
    'fields': [
        {'fieldId': 'SbId', 'primaryKey': True, 'dataType': 'int', 'label': 'Id', 'mandatory': False},
        {'fieldId': 'StId', 'primaryKey': False, 'dataType': 'int', 'label': 'Type', 'mandatory': True},
        {'fieldId': 'Ds', 'dataType': 'string', 'label': 'Omschrijving', 'mandatory': True, 'length': 10},
        {'fieldId': 'Bl', 'dataType': 'boolean', 'label': 'Bijlage', 'mandatory': False},
        {'fieldId': 'Am', 'dataType': 'decimal', 'label': 'Bedrag', 'mandatory': False, 'decimals': 2},
        {
            'fieldId': 'St',
            'dataType': 'string',
            'label': 'Status',
            'mandatory': False,
            'values': [{'id': 'A', 'description': 'Actief'}, {'id': 'I', 'description': 'Inactief'}],
        },
    ],
    'objects': [
        {
            'name': 'KnSubjectLink',
            'fields': [
                {'fieldId': 'DoCRM', 'dataType': 'boolean', 'mandatory': False},
                {'fieldId': 'ToPE', 'dataType': 'boolean', 'mandatory': True},
            ],
            'objects': [],
        }
    ],
}


def make_settings(**overrides: Any) -> Settings:  # noqa: ANN401
    """Build settings for a fictional OAuth app connector with an in-memory store, without reading a ``.env`` file."""
    values: dict[str, Any] = {
        'member_id': '12345',
        'oauth_client_id': 'connector-id',
        'oauth_client_secret': CLIENT_SECRET,
        'public_url': PUBLIC_URL,
        'encryption_key': ENCRYPTION_KEY,
        'database_path': ':memory:',
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def as_jan() -> Authorization:
    """Authorize a call as a fictional logged-in user."""
    return Authorization(header=JANS_HEADER, subject=JAN)


@pytest.fixture(autouse=True)
def isolated_afas_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the AFAS_* variables a developer's shell or .env may hold, so tests only see what they set themselves."""
    for name in [key for key in os.environ if key.startswith('AFAS_')]:
        monkeypatch.delenv(name)


@pytest.fixture
def oauth_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Configure a fictional OAuth app connector through AFAS_* variables, in a directory without a .env file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('AFAS_MEMBER_ID', '12345')
    monkeypatch.setenv('AFAS_OAUTH_CLIENT_ID', 'connector-id')
    monkeypatch.setenv('AFAS_OAUTH_CLIENT_SECRET', CLIENT_SECRET)
    monkeypatch.setenv('AFAS_PUBLIC_URL', PUBLIC_URL)
    monkeypatch.setenv('AFAS_ENCRYPTION_KEY', ENCRYPTION_KEY)


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio only."""
    return 'asyncio'


@pytest.fixture
def settings() -> Settings:
    """Settings for a fictional production environment."""
    return make_settings()


@pytest.fixture
def update_schema() -> dict[str, Any]:
    """A small UpdateConnector schema in the shape ``metainfo/update/{id}`` returns."""
    return UPDATE_SCHEMA
