"""Run the documented quickstart against the real authentication boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
@pytest.mark.parametrize("with_config", [True, False], ids=["yaml", "defaults"])
def test_quickstart_completes(tmp_path, with_config):
    script = tmp_path / "quickstart.py"
    script.write_text(
        (ROOT / "examples/quickstart.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    if with_config:
        (tmp_path / "config.yml").write_text(
            (ROOT / "examples/config.yml").read_text(encoding="utf-8"), encoding="utf-8"
        )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    for marker in ("[add]", "[search]", "[get]", "[update]", "[evolve]", "[admin]", "[audit]"):
        assert marker in output, f"Missing {marker}: {output}"
    assert "Alice 喜欢在早上喝美式咖啡，不加糖。" in output, output
    assert "未开启归属判定，跳过" in output, output
