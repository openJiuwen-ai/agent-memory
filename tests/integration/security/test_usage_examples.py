# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""运行指南引用的真实示例，防止文档、配置与入口契约分别漂移。"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GUIDE = ROOT / "docs/zh/API文档/security.md"
EXAMPLES = ROOT / "examples/security"
SCRIPTS = [
    ("quickstart.py", []),
    ("api_key.py", []),
    ("key_revocation.py", []),
    ("grant_http.py", []),
    ("grant_sdk.py", []),
    ("space.py", []),
    ("delegation.py", []),
    ("gateway.py", ["--self-test"]),
    ("encryption.py", []),
    ("audit.py", []),
    ("mcp_stdio.py", []),
]


@pytest.mark.integration
@pytest.mark.parametrize(("filename", "arguments"), SCRIPTS, ids=[s[0] for s in SCRIPTS])
def test_repository_security_example(filename, arguments):
    if filename == "mcp_stdio.py":
        pytest.importorskip("mcp", reason="Install the mcp extra; security-examples CI requires it")
    env = os.environ.copy()
    paths = [str(ROOT), str(ROOT / "jiuwen_memory_entry/core")]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / filename), *arguments],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 0, f"{filename} failed:\n{result.stdout}\n{result.stderr}"
    assert "PASS " in result.stdout, f"{filename} did not report its verification result"


@pytest.mark.unit
def test_documented_audit_cli_command_is_accepted():
    from jiuwen_memory_entry.cli.__main__ import build_parser
    from jiuwen_memory_entry.cli.commands import build_payload

    guide = GUIDE.read_text(encoding="utf-8")
    commands = []
    for block in re.findall(r"^```bash\n(.*?)^```", guide, re.M | re.S):
        for line in block.replace("\\\n", " ").splitlines():
            if line.startswith("scripts/run-cli.sh ") and " verify_audit " in line:
                commands.append(shlex.split(line)[1:])
    assert len(commands) == 1, "Keep one complete, executable audit CLI command in the guide"
    arguments = build_parser().parse_args(commands[0])
    assert build_payload("verify_audit", arguments) == {
        "after_sequence": 0,
        "page_size": 1000,
        "max_samples": 20,
        "anchor_policy": "if_configured",
    }


@pytest.mark.unit
def test_guide_examples_and_relative_links_exist():
    guide = GUIDE.read_text(encoding="utf-8")
    for filename, _ in SCRIPTS:
        assert f"examples/security/{filename}" in guide, f"Missing guide entry: {filename}"
    for target in re.findall(r"\]\(([^)]+)\)", guide):
        if not target.startswith(("http://", "https://", "#")):
            assert (GUIDE.parent / target.split("#")[0]).exists(), f"Broken guide link: {target}"
