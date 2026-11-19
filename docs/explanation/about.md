# About AFAS MCP Server

AFAS Profit holds the HR, payroll, finance and CRM data of tens of thousands of organisations, and its REST
connectors are how that data leaves the system. The Model Context Protocol is how AI assistants call tools.
This project is the small, honest piece between the two.

## What problem does it solve?

People who work with AFAS data spend their days answering questions such as "which employees changed cost centre
last month" or "which debtors have overdue invoices in Utrecht". Each answer means finding the right GetConnector,
knowing its field ids, writing a filter in AFAS's numeric operator syntax and paging through the result. An
assistant can do that work if it can see the connectors. Existing options were hosted proxies that put a third
party between you and your ERP, or building a one-off integration. This server runs where you run it, lets every
user log in with their own AFAS account, and is small enough to audit in an afternoon.

## How it works, at a glance

- `configuration.py` reads `AFAS_*` settings and derives the AFAS REST and OAuth URLs from them.
- `client.py` is a thin asynchronous client for the REST services: metainfo, GetConnectors, UpdateConnectors, and
  error decoding including the base64 `X-PROFIT-ERROR` header.
- `filters.py` turns readable filter objects into the `filterfieldids`/`filtervalues`/`operatortypes` query
  string, or into `filterjson` when a value contains a separator.
- `validation.py` checks an UpdateConnector payload against the schema AFAS publishes for it, offline.
- `server.py` registers the tools with the MCP SDK and translates AFAS errors into tool errors the model can read.
- `oauth.py` is the login: an OAuth 2.0 authorization server for MCP clients that sends each user to AFAS and
  keeps the AFAS tokens it gets back.
- `storage.py` is the encrypted SQLite store behind it.
- `__main__.py` is the command line: serve over streamable HTTP, or `--check` the settings.

One AFAS client lives for the lifetime of the server and is shared by all tool calls; who a call is made as is
resolved per request from the caller's login.

## Key design decisions

- **Read-only unless asked.** The write tools are not merely refused, they are not registered without
  `AFAS_ALLOW_WRITES=true`. A model cannot be talked into calling a tool it cannot see, and an operator can tell
  from the tool list alone what a server can do. The alternative, a permission flag checked inside each tool, would
  leave the tools visible and the decision to a prompt.
- **No bundled connector knowledge.** The server discovers connectors and fields through metainfo at call time
  rather than shipping a catalogue. AFAS environments differ in which connectors exist and what they contain; a
  catalogue would be wrong somewhere on day one. The price is one extra call per describe, which AFAS answers fast.
- **Validate before writing, on the client side.** AFAS validates too, but its error messages come after the
  request and one at a time. Checking the payload against the schema first lets the model fix every problem at
  once and, in `afas_validate_payload`, lets a user see the exact body before anything is sent. It is a safety net,
  not a guarantee: business rules only AFAS knows still apply, so `validate: false` exists for schemas that are
  incomplete.
- **Filters by name, grouped by number.** Operators are words (`contains`) rather than AFAS codes (6), and the
  AND/OR structure is expressed with a `group` number per filter. That is a flat list a model produces reliably,
  and it maps one-to-one onto AFAS's comma-and-semicolon syntax. Nested boolean trees were considered and
  rejected as harder for models to emit correctly and no more expressive than what AFAS supports.
- **Pass AFAS metainfo through unchanged.** Describe tools return AFAS's own JSON rather than a normalised shape,
  so documentation AFAS publishes about a field applies verbatim and nothing is lost in translation.
- **AFAS OAuth 2.0 per user, and nothing else.** There is no shared credential that reads AFAS on everyone's
  behalf. The server is an OAuth 2.0 authorization server for MCP clients and delegates the login to AFAS through
  the authorization code flow with PKCE: each user logs in at AFAS, and AFAS's own authorisation and filter
  authorisation decide what that user's assistant can see. A shared classic token would make every user as
  powerful as the token, leave the HTTP endpoint without authentication of its own, and stop working when AFAS
  retires classic tokens in 2027.
- **AFAS tokens never leave the server.** It keeps them itself, encrypted, and hands MCP clients opaque tokens
  of its own. A leaked MCP token can be revoked without touching AFAS, and it is worthless against AFAS directly.
- **Streamable HTTP only, without sessions.** A browser login needs a server with a URL, so there is no stdio
  transport. Every request carries its own token and the server keeps nothing in memory between requests, so a
  restart is invisible to clients.
- **A consent page before AFAS.** MCP clients register themselves, so anyone can create one. Without a page that
  names the client and waits for a click, a link could complete silently on a victim's existing AFAS session and
  deliver their access to the attacker's client. The page, and the cookie that ties a login attempt to the
  browser that approved it, are what the MCP security guidance asks of a server that proxies another login.
- **SQLite for AFAS OAuth 2.0.** The server has to remember registered clients and tokens across restarts. One file
  on a volume does that without a second service to run, secure and back up; keys are stored hashed and values
  encrypted, so a copy of the file is not a set of credentials.

## Where it comes from

This project started as a fork of
[schubergphilis/afas_mcp_server](https://github.com/schubergphilis/afas_mcp_server) by Ellert van der Vecht. The
original connects to AFAS with one classic app connector token and runs over stdio next to a desktop client. This
project replaced that with AFAS OAuth 2.0 per user and a shared deployment, and is developed independently. The
tools, the filter syntax and the payload validation come from the original.

The rules for changing the code, for people and for AI agents alike, are in
[`AGENTS.md`](https://github.com/chruisss/afas_ai_mcp_connector/blob/main/AGENTS.md). Dependencies are pinned exactly in
`uv.lock`, and the container image installs from that file.

## License

Apache-2.0. Every runtime dependency in the lockfile is MIT, BSD-3-Clause, Apache-2.0, PSF-2.0
or MIT-0, so nothing forces a copyleft license and any permissive one would have been possible.
The original project chose Apache-2.0 over MIT because it adds an explicit patent grant; this project keeps
that license and the original copyright notice.
AFAS and AFAS Profit are trademarks of AFAS Software B.V.; this project is not affiliated with
AFAS.

## Scope and non-goals

- Not a general AFAS SDK. The client covers what the tools need; use it if it fits, but completeness is not a goal.
- No SOAP, no Insite or Outsite, no file, image or report connectors in this version. They are candidates once
  someone needs them.
- No caching of rows or schemas. AFAS is the source of truth, and staleness in an assistant's answer is worse than
  an extra call.
- No classic app connector tokens and no stdio transport. Use the
  [original project](https://github.com/schubergphilis/afas_mcp_server) for a single-user setup on a token.
- No users, roles or permissions of its own. Who may log in and what they may see is configured in AFAS.
- No TLS. The server speaks plain HTTP; a reverse proxy terminates TLS in front of it.

## See also

- [Tools](../reference/tools.md) and [Configuration](../reference/configuration.md).
