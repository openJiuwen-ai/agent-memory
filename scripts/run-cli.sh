#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Convenience wrapper for the agent-memory CLI surface (DESIGN.md).
# Runs the CLI as a script so its sibling import roots resolve correctly.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${REPO_ROOT}:${REPO_ROOT}/bootstrap/core${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 "${REPO_ROOT}/bootstrap/cli/__main__.py" "$@"
