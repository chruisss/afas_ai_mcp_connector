#!/bin/sh
# Build the server image, start it with fictional settings and check that it is healthy, that the
# MCP endpoint refuses a request without a login, and that it refuses to start without settings.
set -eu
cd "$(dirname "$0")/.."

IMAGE=afas-mcp-server:smoke-test
NAME=afas-mcp-smoke-test
PORT=${SMOKE_TEST_PORT:-18000}

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build --quiet --tag "$IMAGE" . >/dev/null

if docker run --rm "$IMAGE" --check >/dev/null 2>&1; then
    echo "FAIL: the server accepted an empty configuration" >&2
    exit 1
fi

# A throwaway key for this run only; it protects nothing.
KEY=$(docker run --rm --entrypoint python "$IMAGE" -c \
    "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")

cleanup
docker run --detach --name "$NAME" --publish "127.0.0.1:$PORT:8000" --read-only --tmpfs /tmp \
    --env AFAS_MEMBER_ID=12345 \
    --env AFAS_OAUTH_CLIENT_ID=smoke-test \
    --env AFAS_OAUTH_CLIENT_SECRET=smoke-test \
    --env AFAS_PUBLIC_URL=https://afas-mcp.example.com \
    --env AFAS_ENCRYPTION_KEY="$KEY" \
    "$IMAGE" >/dev/null

attempts=0
until curl --silent --fail "http://127.0.0.1:$PORT/healthz" >/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 30 ]; then
        echo "FAIL: /healthz did not answer within 30 seconds" >&2
        docker logs "$NAME" >&2
        exit 1
    fi
    sleep 1
done

status=$(curl --silent --output /dev/null --write-out '%{http_code}' --request POST "http://127.0.0.1:$PORT/mcp")
if [ "$status" != "401" ]; then
    echo "FAIL: /mcp answered $status without a login, expected 401" >&2
    exit 1
fi

user=$(docker exec "$NAME" id -u)
if [ "$user" = "0" ]; then
    echo "FAIL: the container runs as root" >&2
    exit 1
fi

echo "OK: image builds, /healthz answers, /mcp requires a login, the container is not root"
