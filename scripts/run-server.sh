#!/usr/bin/env bash
# Convenience wrapper for the agent-memory HTTP server surface (DESIGN.md).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${REPO_ROOT}:${REPO_ROOT}/bootstrap/core${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 "${REPO_ROOT}/bootstrap/http_server/__main__.py" "$@"
