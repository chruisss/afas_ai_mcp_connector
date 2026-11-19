"""Tests for the command line entry point."""

import json
from pathlib import Path
from typing import Any

import pytest

from afas_mcp_server import __main__ as cli
from tests.conftest import PUBLIC_URL


class RecordingServer:
    """Stands in for the MCP server so the CLI can be exercised without opening a port."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, transport: str, **kwargs: Any) -> None:  # noqa: ANN401
        """Record how the server was asked to run."""
        self.calls.append((transport, kwargs))


def test_parser_defaults() -> None:
    """Out of the box the server listens on this machine only; a container or proxy opens it up deliberately."""
    args = cli.build_parser().parse_args([])
    assert args.check is False
    assert (args.host, args.port, args.path) == ('127.0.0.1', 8000, '/mcp')


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """--version prints the installed version and exits cleanly."""
    with pytest.raises(SystemExit) as exit_info:
        cli.main(['--version'])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith('afas-mcp-server ')


def test_missing_configuration_explains_and_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing settings are a configuration error, named on stderr by variable with a pointer to .env.example."""
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 2
    err = capsys.readouterr().err
    assert 'Invalid AFAS configuration' in err
    assert 'AFAS_OAUTH_CLIENT_ID: Field required' in err
    assert 'AFAS_ENCRYPTION_KEY: Field required' in err
    assert '.env.example' in err


@pytest.mark.usefixtures('oauth_environment')
def test_check_prints_what_to_register(capsys: pytest.CaptureFixture[str]) -> None:
    """--check validates the settings and shows the redirect URL the AFAS app connector needs, without calling AFAS."""
    assert cli.main(['--check']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        'base_url': 'https://12345.rest.afas.online/profitrestservices',
        'writes_enabled': False,
        'authentication': 'afas-oauth2',
        'afas_redirect_url': f'{PUBLIC_URL}/oauth/afas/callback',
        'afas_authorize_url': 'https://12345.rest.afas.online/profitrestservices/oauth/authorize',
        'afas_token_url': 'https://12345.rest.afas.online/profitrestservices/oauth/token',
    }


@pytest.mark.usefixtures('oauth_environment')
def test_serves_streamable_http_without_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every request carries its own token, so the server keeps no sessions; it binds to this machine by default."""
    server = RecordingServer()
    paths: list[str] = []
    monkeypatch.setattr(cli, 'build_server', lambda _settings, mcp_path: paths.append(mcp_path) or server)
    assert cli.main([]) == 0
    assert paths == ['/mcp']
    expected = {
        'host': '127.0.0.1',
        'port': 8000,
        'streamable_http_path': '/mcp',
        'stateless_http': True,
        'json_response': True,
    }
    assert server.calls == [('streamable-http', expected)]


@pytest.mark.usefixtures('oauth_environment')
def test_bind_address_port_and_path_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A container binds to every interface, and a proxy may want another path."""
    server = RecordingServer()
    monkeypatch.setattr(cli, 'build_server', lambda _settings, **_options: server)
    assert cli.main(['--host', '0.0.0.0', '--port', '9000', '--path', '/afas']) == 0  # noqa: S104
    _transport, options = server.calls[0]
    assert (options['host'], options['port'], options['streamable_http_path']) == ('0.0.0.0', 9000, '/afas')  # noqa: S104
