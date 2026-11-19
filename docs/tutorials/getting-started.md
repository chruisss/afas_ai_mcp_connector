# Getting started with AFAS MCP Server

In this tutorial you deploy the server for an AFAS **test** environment, log in at AFAS from an AI assistant and
let it answer its first question from your data. By the end you have a running, read-only MCP server that every
colleague in the right AFAS authorisation group can use with their own account.

## You need

- An AFAS Profit test environment, and rights to create an app connector in it.
- The environment number from your AFAS Online URL: `12345` in `https://12345.afasonline.com`.
- A machine with Docker and Docker Compose, and a checkout of this repository on it.
- A hostname that reaches that machine over `https`, for example `https://afas-mcp.example.com`. A reverse proxy
  such as Caddy, Traefik or nginx provides the TLS.
- [Claude Code](https://code.claude.com/docs) or another MCP client that supports remote servers with OAuth.

## Step 1: Create the OAuth app connector in AFAS

1. In AFAS Profit, add an app connector with authentication type **OAuth**. Make it a confidential client (in
   Profit 9: type *Webapplicatie*) that uses the **authorization code** flow with a **client secret**.
2. Set the redirect URL to `https://afas-mcp.example.com/oauth/afas/callback`, with your own hostname. It has to
   match exactly.
3. Link one or more GetConnectors the assistant may read, for example `Profit_Employees`.
4. Link the authorisation group whose members may log in, and make sure your own user is in it.
5. Note the client id and the client secret.

## Step 2: Write the settings

Generate the key that encrypts the tokens the server stores:

```bash
python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

Copy `.env.example` to `.env` next to `docker-compose.yaml` and fill in:

```bash
AFAS_MEMBER_ID=12345
AFAS_ENVIRONMENT=test
AFAS_OAUTH_CLIENT_ID=the-client-id-from-step-1
AFAS_OAUTH_CLIENT_SECRET=the-client-secret-from-step-1
AFAS_PUBLIC_URL=https://afas-mcp.example.com
AFAS_ENCRYPTION_KEY=the-key-you-just-generated
```

Leave `AFAS_ALLOW_WRITES` at `false`.

## Step 3: Start the server

```bash
docker compose up -d --build
```

The container listens on port 8000 and publishes nothing on the host. Point your reverse proxy at port 8000 of the
`afas-mcp` service and let it terminate TLS for your hostname;
[Deploy with Docker Compose](../how-to/deploy-with-docker-compose.md) shows the two usual ways. Then check that the server answers:

```bash
curl https://afas-mcp.example.com/healthz
```

You should see `{"status": "ok"}`.

## Step 4: Connect and log in

Add the server to Claude Code by URL:

```bash
claude mcp add --transport http afas https://afas-mcp.example.com/mcp
```

Start Claude Code and run `/mcp` to authenticate. A browser opens:

1. A page from the server names the application that asks for access. Choose **Continue to AFAS**.
2. Log in at AFAS with your own account.
3. The browser returns to Claude Code, which reports that the server is connected.

## Step 5: Ask the first question

Ask:

> Which AFAS environment are you connected to, and which connectors can you read?

The assistant calls `afas_connection_info` and `afas_list_connectors`. The answer names your test environment,
shows `authentication: afas-oauth2`, and lists the GetConnectors you linked in step 1. Then try:

> Show the first five rows of Profit_Employees.

In the container log (`docker compose logs afas-mcp`) every AFAS call appears with your user name:

```text
INFO afas_mcp_server.audit: GET connectors/Profit_Employees as jan@example.com
```

## What you built

One server that holds no shared AFAS credential. Each colleague who adds the URL logs in with their own AFAS
account, and AFAS decides what their assistant can see.

## Next steps

- [Deploy with Docker Compose](../how-to/deploy-with-docker-compose.md) for the production setup.
- [Connect an MCP client](../how-to/connect-an-mcp-client.md) for clients other than Claude Code.
- [Operate the server](../how-to/operate-the-server.md) for logs, backups and rotating the secret.
- [Enable writes safely](../how-to/enable-writes-safely.md) when reading is not enough.
- [About AFAS MCP Server](../explanation/about.md) explains why the login works the way it does.
