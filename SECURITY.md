# Security policy

## What this project handles

The server is an OAuth 2.0 authorization server for MCP clients and a client of AFAS OAuth 2.0. It
holds the client secret of an AFAS app connector and, encrypted in its own store, the AFAS access
and refresh tokens of every logged-in user, and it forwards tool calls to the AFAS Profit REST
services with them. A vulnerability in this project could therefore expose or change ERP, HR and
payroll data. We take reports seriously and answer quickly.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose
**Report a vulnerability**. If that is unavailable, email Chris Schrijver at chris@tjermo.nl with
"afas_mcp_server security" in the subject.

Include what you found, how to reproduce it, and the version (`afas-mcp-server --version`).
You will get an acknowledgement within three working days and a fix or a clear plan within
thirty days for confirmed issues. We will credit you in the fix unless you prefer
otherwise.

## Scope

In scope: this repository's code, its default configuration, its documentation where it gives
unsafe advice, and its container image definition.

Out of scope: AFAS Profit itself (report to AFAS Software), the MCP clients that connect to the
server, and deployments that expose the server without TLS in front of it.

## Supported versions

The `main` branch receives fixes. Older revisions are not patched; upgrade instead.
