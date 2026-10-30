# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""临时 SQLite 完整性链，实际 HTTP 和 CLI 校验，全部密钥仅在本进程及子进程。"""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from _support import post, running

with TemporaryDirectory(prefix="agent-memory-audit-") as state:
    os.environ["AGENT_MEMORY_ROOT_API_KEY"] = secrets.token_urlsafe(32)
    os.environ["AGENT_MEMORY_API_KEY"] = os.environ["AGENT_MEMORY_ROOT_API_KEY"]
    os.environ["AGENT_MEMORY_AUDIT_ROOT_KEY"] = secrets.token_hex(32)
    os.environ["AGENT_MEMORY_AUDIT_DB"] = str(Path(state) / "audit.sqlite3")
    with running("audit.yml") as (_, base):
        post(
            base,
            "add",
            os.environ["AGENT_MEMORY_API_KEY"],
            {
                "content": "audit test",
                "scope": {"org": "system", "user": "root"},
            },
        )
        parameters = {
            "after_sequence": 0,
            "page_size": 1000,
            "max_samples": 20,
            "anchor_policy": "if_configured",
        }
        result = post(base, "verify_audit", os.environ["AGENT_MEMORY_API_KEY"], parameters)
        assert result["status"] == "clean", result
        assert result["anchor"]["checked"] is False, result
        print("PASS audit HTTP: clean; external anchor not checked")
        checked = subprocess.run(
            [
                sys.executable,
                "-m",
                "jiuwen_memory_entry.cli",
                "--server",
                base,
                "verify_audit",
                "--after_sequence",
                "0",
                "--page_size",
                "1000",
                "--max_samples",
                "20",
                "--anchor_policy",
                "if_configured",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert json.loads(checked.stdout)["status"] == "clean", checked.stdout
        print("PASS audit CLI: underscore arguments accepted; status clean")
