#!/usr/bin/env bash
# Convenience wrapper for the agent-memory MCP server surface (needs: pip install ".[mcp]").
# stdio by default; set MCP_TRANSPORT=http (+ MCP_PORT) for Streamable HTTP.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${REPO_ROOT}/src:${REPO_ROOT}/bootstrap/core${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 "${REPO_ROOT}/bootstrap/mcp_server/__main__.py" "$@"
