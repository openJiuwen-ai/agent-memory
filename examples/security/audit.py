# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""临时 SQLite 完整性链，实际 HTTP 和 CLI 校验，全部密钥仅在本进程及子进程。"""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from _support import logger, post, running

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
        if result["status"] != "clean":
            raise RuntimeError("HTTP audit verification must return clean")
        if result["anchor"]["checked"] is not False:
            raise RuntimeError("No external anchor should be checked in this example")
        logger.info("PASS audit HTTP: clean; external anchor not checked")
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
        if json.loads(checked.stdout)["status"] != "clean":
            raise RuntimeError("CLI audit verification must return clean")
        logger.info("PASS audit CLI: underscore arguments accepted; status clean")
