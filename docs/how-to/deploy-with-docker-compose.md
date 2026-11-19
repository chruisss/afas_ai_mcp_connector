# Deploy with Docker Compose

Run the server from this repository's `docker-compose.yaml` behind the reverse proxy you already use.

## Before you start

- You created the OAuth app connector in AFAS and have its client id and secret
  (step 1 of [Getting started](../tutorials/getting-started.md)).
- You have a host with Docker and Docker Compose, and a checkout of this repository on it.
- You have a hostname for the server, e.g. `afas-mcp.example.com`, and a reverse proxy that terminates TLS for it.
- The redirect URL of the app connector is `https://afas-mcp.example.com/oauth/afas/callback`.

## Steps

1. Generate an encryption key and keep it with your other secrets:

   ```bash
   python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
   ```

2. Copy `.env.example` to `.env` next to `docker-compose.yaml` and set:

   | Variable | Value |
   | --- | --- |
   | `AFAS_MEMBER_ID` | Your AFAS environment number. |
   | `AFAS_ENVIRONMENT` | `test` to start with; `production` later. |
   | `AFAS_OAUTH_CLIENT_ID` | The client id of the app connector. |
   | `AFAS_OAUTH_CLIENT_SECRET` | The client secret of the app connector. |
   | `AFAS_PUBLIC_URL` | `https://afas-mcp.example.com`, without a trailing slash and without a port. |
   | `AFAS_ENCRYPTION_KEY` | The key from step 1. |

   If your platform has its own secret store, set the variables there instead of in a file.

3. Let the reverse proxy reach the container. The compose file publishes no port on the host; the container
   listens on port 8000. Choose what fits your proxy:

   - **Proxy in Docker** (Traefik, Caddy, nginx in a container): attach the `afas-mcp` service to the proxy's
     network and route the hostname to port 8000.
   - **Proxy on the host**: publish the port on the loopback interface only, in a
     `docker-compose.override.yaml` next to the compose file:

     ```yaml
     services:
       afas-mcp:
         ports:
           - "127.0.0.1:8000:8000"
     ```

     and proxy the hostname to `http://127.0.0.1:8000`.

   Do not publish the port on a public interface: the server speaks plain HTTP.

4. Start the server:

   ```bash
   docker compose up -d --build
   ```

## Verify

- `https://afas-mcp.example.com/healthz` answers `{"status": "ok"}`, and `docker compose ps` shows the container
  as healthy.
- Adding `https://afas-mcp.example.com/mcp` to an MCP client opens the consent page and then the AFAS login
  (see [Connect an MCP client](connect-an-mcp-client.md)).

## What the compose file sets up

- A named volume, `afas-mcp-data`, mounted on `/data`. It holds the SQLite file with registered MCP clients and
  the encrypted AFAS tokens, and survives rebuilds.
- A container that runs as a non-root user on a read-only filesystem, with all capabilities dropped. Only
  `/data` and `/tmp` are writable.
- No published host port.

Run one replica: the SQLite file should have one owner.

## See also

- [Operate the server](operate-the-server.md) for what to do after the deploy.
- [Configuration](../reference/configuration.md) for every variable.
