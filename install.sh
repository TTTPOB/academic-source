#!/usr/bin/env bash
# Install this checkout, never an upstream package release.
set -euo pipefail
if ! command -v uv >/dev/null 2>&1; then
  echo "Install uv first: https://docs.astral.sh/uv/" >&2
  exit 1
fi
cd "$(dirname "$0")"
uv sync --frozen --extra vpnsci --extra fast
printf '\nReady: uv run academic-source serve --host 127.0.0.1 --port 8000\n'
printf 'MCP stdio: uv run academic-source mcp\n'
