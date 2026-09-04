#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Convenience wrapper for the agent-memory HTTP server surface (DESIGN.md).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${REPO_ROOT}:${REPO_ROOT}/jiuwen_memory_entry/core${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m jiuwen_memory_entry.http_server "$@"
