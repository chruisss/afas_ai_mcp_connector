# Operate the server

Keep a deployed server healthy: read its logs, rotate its secrets, back it up and limit who can reach it.

Where a step says *apply the settings*, change the variable in `.env` and run `docker compose up -d`.

## See who did what

Every AFAS call is logged on the logger `afas_mcp_server.audit`, with the user it was made as:

```text
INFO afas_mcp_server.audit: GET connectors/Profit_Employees as jan@example.com
```

The user is read from the AFAS access token; when the token does not name one, the line says `unknown-user`.
Filter values and row contents are never logged. Logins appear under `afas_mcp_server.oauth` as
`jan@example.com logged in through client <id>`.

Follow them with `docker compose logs -f afas-mcp`.

## Rotate the client secret

From Profit 9 the client secret of an app connector expires.

1. Create a new secret on the app connector in AFAS before the old one expires.
2. Set `AFAS_OAUTH_CLIENT_SECRET` to the new value and apply the settings.

Users stay logged in.

## Rotate the encryption key

Set a new `AFAS_ENCRYPTION_KEY` and apply the settings. Everything stored with the old key becomes unreadable, which the
server treats as not being logged in: every user logs in again and every MCP client registers again. Nothing else
is lost. Do this when the key may have leaked.

## Log a user out

- **One client of one user:** the user disconnects the server in their MCP client, which revokes its tokens when
  the client supports revocation.
- **A user who left or was blocked:** block the user or remove them from the authorisation group in AFAS. AFAS
  refuses the next token renewal, which happens within the hour, and the server then forgets the login.
- **Everyone, now:** rotate the encryption key.

## Back up

The volume `afas-mcp-data` holds one SQLite file. Losing it means every user logs in again; no AFAS data lives
there. Back it up only if you want logins to survive a server move, and store the backup apart from
`AFAS_ENCRYPTION_KEY`: together they are the AFAS tokens of every logged-in user.

## Limit who can reach the server

Anyone who can reach the URL can register an MCP client and open the consent page; only AFAS users in the linked
authorisation group get past the AFAS login. To expose less:

- Put the hostname behind a VPN or an IP allowlist at the proxy.
- Rate-limit `/register` at the proxy.
- Link only the GetConnectors the assistant needs to the app connector.

## Move from test to production

Create a second app connector in the production environment, then change `AFAS_ENVIRONMENT`, the client id and
the client secret, and apply the settings. Tokens from the test environment stop working at AFAS and users log in again.

## Upgrade

Pull the new revision and run `docker compose up -d --build`. Sessions are not kept in memory, so clients do not notice the restart.

## See also

- [Configuration](../reference/configuration.md) for every variable and HTTP path.
- [About AFAS MCP Server](../explanation/about.md) for the reasoning behind the token store.
