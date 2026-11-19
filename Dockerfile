# The server image, for running afas-mcp-server over streamable HTTP (see docker-compose.yaml).

# Build stage: install the locked runtime dependencies and the package into a virtualenv.
# The uv tag floats; what gets installed is fixed by `uv.lock` (versions and hashes).
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Runtime stage: Python, the virtualenv and nothing else.
FROM python:3.13-slim-trixie

# Not root, and with a fixed uid so the owner of the data volume is predictable.
RUN useradd --uid 10001 --create-home afas && mkdir /data && chown 10001:10001 /data

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    AFAS_DATABASE_PATH=/data/afas-mcp-server.sqlite3

USER 10001
# An empty working directory: the server reads `.env` from where it starts, and here there is none.
WORKDIR /home/afas
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

ENTRYPOINT ["afas-mcp-server"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
