"""Command-line entry point, installed as ``afas-mcp-server`` and reachable as ``python -m afas_mcp_server``."""

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from .configuration import Settings
from .server import AUTHENTICATION, build_server, package_version

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8000
DEFAULT_PATH = '/mcp'
EXIT_OK = 0
EXIT_CONFIGURATION_INVALID = 2
HTTP_TRANSPORT = 'streamable-http'
LOG_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'


def build_parser() -> argparse.ArgumentParser:
    """Describe the command line."""
    parser = argparse.ArgumentParser(
        prog='afas-mcp-server',
        description=(
            'MCP server for AFAS Profit over streamable HTTP; every user logs in at AFAS with OAuth 2.0. '
            'Settings come from AFAS_* environment variables or .env.'
        ),
    )
    parser.add_argument('--version', action='version', version=f'%(prog)s {package_version()}')
    parser.add_argument('--host', default=DEFAULT_HOST, help=f'Bind address (default {DEFAULT_HOST}).')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'Port to listen on (default {DEFAULT_PORT}).')
    parser.add_argument('--path', default=DEFAULT_PATH, help=f'URL path of the MCP endpoint (default {DEFAULT_PATH}).')
    parser.add_argument(
        '--check',
        action='store_true',
        help='Validate the settings, print the redirect URL to register on the AFAS app connector and exit.',
    )
    return parser


def configure_logging(level: str) -> None:
    """Log to stderr, where a container runtime collects it."""
    logging.basicConfig(level=level, stream=sys.stderr, format=LOG_FORMAT)


def load_settings() -> Settings | None:
    """Read the settings, explaining on stderr what is missing when they are invalid.

    Returns:
        The settings, or None when they could not be validated.
    """
    try:
        return Settings()
    except ValidationError as error:
        problems = '; '.join(
            f'{"".join(f"AFAS_{part}".upper() for part in item["loc"]) or "settings"}: {item["msg"]}'
            for item in error.errors()
        )
        sys.stderr.write(f'Invalid AFAS configuration: {problems}\nSee .env.example for the variables to set.\n')
        return None


def run_check(settings: Settings) -> int:
    """Print what the settings resolve to and what to register on the AFAS app connector.

    Nothing is sent to AFAS: there is no credential to try until a user logs in.

    Args:
        settings: The validated settings.

    Returns:
        The process exit code, always 0.
    """
    report: dict[str, Any] = {
        'base_url': settings.rest_base_url,
        'writes_enabled': settings.allow_writes,
        'authentication': AUTHENTICATION,
        'afas_redirect_url': settings.oauth_callback_url,
        'afas_authorize_url': settings.oauth_authorize_url,
        'afas_token_url': settings.oauth_token_url,
    }
    sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    return EXIT_OK


def serve(settings: Settings, args: argparse.Namespace) -> int:
    """Build the server and serve it over streamable HTTP until the process is stopped.

    Args:
        settings: The validated settings.
        args: The parsed command line.

    Returns:
        The process exit code.
    """
    server = build_server(settings, mcp_path=args.path)
    # Every request carries its own token, so the server keeps no sessions: a restart is invisible to clients.
    server.run(
        HTTP_TRANSPORT,
        host=args.host,
        port=args.port,
        streamable_http_path=args.path,
        stateless_http=True,
        json_response=True,
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments without the program name; defaults to the process arguments.

    Returns:
        The process exit code.
    """
    args = build_parser().parse_args(argv)
    settings = load_settings()
    if settings is None:
        return EXIT_CONFIGURATION_INVALID
    configure_logging(settings.python_log_level)
    if args.check:
        return run_check(settings)
    return serve(settings, args)


if __name__ == '__main__':
    sys.exit(main())
