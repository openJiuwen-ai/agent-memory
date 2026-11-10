from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PAGE = ROOT / "jiuwen_memory_entry" / "portal" / "index.html"
SCRIPT = Path(__file__).with_name("portal_frontend_test.cjs")


@pytest.mark.unit
def test_portal_frontend_keeps_drafts_and_uses_fresh_detail() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the Portal frontend regression")

    result = subprocess.run(
        [node, str(SCRIPT), str(PAGE)],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
