# Configuration

The server is configured through `AFAS_*` environment variables. A `.env` file in the working directory is read
too; variables set in the process environment take precedence over it. Only the bind address, port and path come
from command-line flags.

## Environment variables

### AFAS OAuth 2.0

All five are required. Every MCP client sends its user through the AFAS login (authorization code flow with
PKCE), and each call reaches AFAS with that user's own access token.

| Variable | Effect |
| --- | --- |
| `AFAS_MEMBER_ID` | AFAS Online environment number, e.g. `12345`. Digits only. Not needed when `AFAS_BASE_URL` is set. |
| `AFAS_OAUTH_CLIENT_ID` | Client id of an AFAS app connector with authentication type OAuth (confidential client, authorization code flow). |
| `AFAS_OAUTH_CLIENT_SECRET` | Client secret of that app connector. |
| `AFAS_PUBLIC_URL` | The `https` URL users and MCP clients reach this server on, e.g. `https://afas-mcp.example.com`. Register `{AFAS_PUBLIC_URL}/oauth/afas/callback` as the redirect URL of the app connector. Plain `http` is accepted on `localhost` only. |
| `AFAS_ENCRYPTION_KEY` | Fernet key (32 random bytes, URL-safe base64) that encrypts the stored AFAS tokens. Changing it logs everyone out. |

Generate an encryption key with:

```bash
python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

### Optional

| Variable | Default | Effect |
| --- | --- | --- |
| `AFAS_ENVIRONMENT` | `production` | `production`, `test` or `accept`; selects the `rest`, `resttest` or `restaccept` hostname. |
| `AFAS_BASE_URL` | | Full REST base URL, e.g. `https://12345.rest.afas.online/profitrestservices`. Overrides `AFAS_MEMBER_ID` and `AFAS_ENVIRONMENT`. Must be `https`, except on `localhost`. |
| `AFAS_DATABASE_PATH` | `afas-mcp-server.sqlite3` | SQLite file holding registered MCP clients and the encrypted AFAS tokens. Put it on persistent storage. The Docker image sets it to `/data/afas-mcp-server.sqlite3`. |
| `AFAS_ALLOW_WRITES` | `false` | Register `afas_insert`, `afas_update` and `afas_delete`. |
| `AFAS_DEFAULT_TAKE` | `100` | Rows per page when `afas_get_rows` is called without `take`. |
| `AFAS_MAX_TAKE` | `1000` | Largest `take` a call may request; larger values are refused with a tool error. |
| `AFAS_TIMEOUT_SECONDS` | `60` | HTTP timeout per AFAS call. |
| `AFAS_LANGUAGE` | `nl-nl` | `Accept-Language` sent to AFAS: `nl-nl`, `nl-be`, `fr-fr`, `de-de` or `en-us`. The consent page is Dutch for `nl-*` and English otherwise. |
| `AFAS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. Logs go to stderr. |

### Derived URLs

| URL | Value |
| --- | --- |
| REST base URL | `https://{AFAS_MEMBER_ID}.{host}.afas.online/profitrestservices`, where `{host}` is `rest`, `resttest` or `restaccept` |
| AFAS authorization endpoint | `{base URL}/oauth/authorize` |
| AFAS token endpoint | `{base URL}/oauth/token` |
| Redirect URL to register in AFAS | `{AFAS_PUBLIC_URL}/oauth/afas/callback` |

## HTTP paths

| Path | Purpose |
| --- | --- |
| `/mcp` | The MCP endpoint (streamable HTTP, stateless, JSON responses). Requires a token issued by this server. |
| `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp` | Discovery documents MCP clients read. |
| `/register`, `/authorize`, `/token`, `/revoke` | The OAuth 2.0 endpoints MCP clients use. These are the server's own, not AFAS's. |
| `/oauth/consent` | The page that asks the user whether the named application may act for them. |
| `/oauth/afas/callback` | The redirect URL of the AFAS app connector: where AFAS sends the user's browser after login. |
| `/healthz` | Answers `{"status": "ok"}` without authentication. |

## Token lifetimes

| Token | Lifetime |
| --- | --- |
| AFAS access token | One hour; the server renews it with the AFAS refresh token shortly before it expires. |
| AFAS login | 30 days from the first login, after which AFAS requires a new login. |
| MCP access token | One hour, and never past the end of the AFAS login. |
| MCP refresh token | Until the end of the AFAS login; single use, replaced at every refresh. |
| MCP client registration | 90 days. |
| Consent page and login attempt | Ten minutes each. |

## Command-line flags

`afas-mcp-server [--host H] [--port P] [--path /mcp] [--check] [--version]`

| Flag | Default | Effect |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. The Docker image passes `0.0.0.0`. |
| `--port` | `8000` | Port to listen on. |
| `--path` | `/mcp` | URL path of the MCP endpoint. |
| `--check` | off | Validate the settings and print a JSON report with the REST base URL, the AFAS OAuth endpoints and the redirect URL to register in AFAS. Nothing is sent to AFAS. |
| `--version` | | Print the version and exit. |

The server speaks plain HTTP; a reverse proxy terminates TLS in front of it.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Normal exit. |
| `2` | Configuration invalid; the missing or malformed variables are named on stderr. |

## See also

- [Tools](tools.md): every tool and its parameters.
