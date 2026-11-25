from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PAGE = ROOT / "jiuwen_memory_entry" / "portal" / "index.html"
SCRIPT = Path(__file__).with_name("portal_frontend_test.cjs")
ROLE_SCRIPT = Path(__file__).with_name("multi_agent_request_ledger_test.cjs")
STATE_SCRIPT = Path(__file__).with_name("multi_agent_view_state_test.cjs")


@pytest.mark.unit
def test_portal_frontend_keeps_drafts_and_uses_fresh_detail() -> None:
    html = PAGE.read_text(encoding="utf-8")
    assert "/portal/multi_agent.css" in html
    assert "/portal/multi_agent.js" in html
    assert (PAGE.parent / "multi_agent.css").is_file()
    assert (PAGE.parent / "multi_agent.js").is_file()


@pytest.mark.unit
def test_multi_agent_frontend_hydrates_missing_sources_and_refreshes_paths() -> None:
    drawer_script = (PAGE.parent / "multi_agent.js").read_text(encoding="utf-8")
    assert "roleRequests: createRoleRequestLedger()" in drawer_script
    assert "const isCurrentRequest = state.roleRequests.begin(duties)" in drawer_script
    assert "function continuityTaskUsable()" in drawer_script
    assert "hasRestrictedContinuityScope" not in drawer_script
    assert "token !== state.roleToken" not in drawer_script
    script = drawer_script
    assert "if (force || needsRecordRead(item))" in script
    assert "syncPathSources(state.detailReference)" in script
    assert "role_catalog" in script
    assert "state.compareDuties" in script
    assert "state.detailStale = referenceChanged(state.detailReference, current)" in script
    assert "state.records" not in script
    assert "bindingSeq" not in script
    assert "data-ma-bind" not in script
    assert '["diagnosis", "testing", "execution"]' not in script
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

    result = subprocess.run(
        [node, str(ROLE_SCRIPT), str(PAGE.parent / "multi_agent.js")],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    result = subprocess.run(
        [node, str(STATE_SCRIPT), str(PAGE.parent / "multi_agent.js")],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
