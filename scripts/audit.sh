#!/bin/sh
# Audit the locked runtime dependencies, exactly as the image installs them, for known vulnerabilities.
set -eu
cd "$(dirname "$0")/.."
requirements=$(mktemp)
trap 'rm -f "$requirements"' EXIT
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file "$requirements" --quiet
uvx pip-audit==2.10.1 --requirement "$requirements" --require-hashes --disable-pip --strict
