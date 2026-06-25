#!/usr/bin/env bash
# Convenience wrapper for the agent-memory MCP server surface (needs: pip install ".[mcp]").
# stdio by default; set MCP_TRANSPORT=http (+ MCP_PORT) for Streamable HTTP.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "${REPO_ROOT}/bootstrap/mcp_server/__main__.py" "$@"
