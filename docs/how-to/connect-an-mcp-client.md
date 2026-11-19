# Connect an MCP client

Add a deployed server to the MCP client you use. Every client needs only the URL of the MCP endpoint; there is
no token or key to hand out, because each user logs in at AFAS.

## Before you start

- The server is running and `https://afas-mcp.example.com/healthz` answers
  (see [Getting started](../tutorials/getting-started.md)).
- Your AFAS user is in the authorisation group linked to the app connector.
- Your client supports remote MCP servers over streamable HTTP with OAuth. Clients that can only start a local
  command over stdio cannot connect.

The examples use `https://afas-mcp.example.com/mcp`; replace the hostname with your own.

## Claude Code

```bash
claude mcp add --transport http afas https://afas-mcp.example.com/mcp
```

Run `/mcp` inside Claude Code and choose the server to log in. Add `--scope project` to share the entry with your
team through `.mcp.json`; the file contains only the URL, so it is safe to commit.

## Claude Desktop and claude.ai

Open *Settings > Connectors*, choose *Add custom connector* and enter the URL. Claude opens the login when you
first connect.

## Cursor

Add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "afas": { "url": "https://afas-mcp.example.com/mcp" }
  }
}
```

## Other clients

Enter `https://afas-mcp.example.com/mcp` as a remote or HTTP MCP server. The client discovers the rest from the
server: it registers itself, opens a browser and receives its tokens.

## What the user sees

1. A page from the server that names the application asking for access and where it will send the user
   afterwards. Continue only if you started the connection yourself.
2. The AFAS login.
3. The client, connected.

The login lasts 30 days, after which the client opens the browser again.

## If it does not connect

| Symptom | Likely cause |
| --- | --- |
| The browser shows "This login attempt was started in another browser" | The link was opened in a different browser than the one that showed the consent page. Start again from the client. |
| The browser shows "This login attempt has expired or was already used" | More than ten minutes passed, or the page was reloaded. Start again from the client. |
| AFAS refuses the login or reports an invalid redirect URL | The redirect URL on the app connector is not exactly `{AFAS_PUBLIC_URL}/oauth/afas/callback`, or the user is not in the linked authorisation group. |
| Tools fail with "your AFAS login has expired; reconnect to log in again" | AFAS refused to renew the session. Reconnect from the client. |

## See also

- [Tools](../reference/tools.md) for what the assistant can call once connected.
- [Operate the server](operate-the-server.md) for the server side of these symptoms.
