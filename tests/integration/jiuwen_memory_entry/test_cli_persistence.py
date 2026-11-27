# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Persistence through independent local CLI processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import indent

import pytest

pytestmark = pytest.mark.integration


def test_cli_reads_sqlite_records_across_processes(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[3]
    entry = repo / "jiuwen_memory_entry"
    env = os.environ.copy()
    env.pop("AGENT_MEMORY_SERVER", None)
    env["PYTHONPATH"] = os.pathsep.join((str(repo), str(entry / "core")))
    env["PYTHONIOENCODING"] = "utf-8"
    scope = json.dumps({"org": "local", "user": "developer"})
    config = tmp_path / "cfg.yml"
    db_path = tmp_path / "db" / "persist.db"
    storage = (
        "kv_store:\n"
        "  default:\n"
        "    target: sqlite\n"
        "    params:\n"
        '      db_path: "./db/persist.db"\n'
    )

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable, str(entry / "cli" / "__main__.py"),
                "--config", str(config), "--auth-mode", "dev", *args,
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )

    add_args = ("add", "--content", "Alice prefers Americano in the morning", "--scope", scope)
    config.write_text(storage, encoding="utf-8")
    rejected = run(*add_args)
    assert rejected.returncode == 2
    assert not rejected.stdout
    assert "unexpected top-level service configuration keys 'kv_store'" in rejected.stderr
    assert "Put kernel configuration under memory_api" in rejected.stderr
    assert "Traceback" not in rejected.stderr
    assert not db_path.exists()

    config.write_text("memory_api:\n" + indent(storage, "  "), encoding="utf-8")
    added = run(*add_args)
    assert added.returncode == 0, added.stderr
    unit_id = json.loads(added.stdout)[0]["id"]
    assert f"KV store backend=SQLiteKVStore path={str(db_path.resolve())!r}" in added.stderr
    assert db_path.is_file()

    listed = run("list", "--scope", scope)
    assert listed.returncode == 0, listed.stderr
    page = json.loads(listed.stdout)
    assert page["count"] == 1
    assert [item["id"] for item in page["items"]] == [unit_id]

    fetched = run("get", "--unit_id", unit_id, "--scope", scope)
    assert fetched.returncode == 0, fetched.stderr
    assert json.loads(fetched.stdout)["id"] == unit_id
