from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.type_def import MD_FILENAME_KEY, MemoryUnit, Scope, Segment
from jiuwen_memory.storage.markdown_impl import experience_blocks
from jiuwen_memory.storage.markdown_impl.local_markdown_store import LocalMarkdownStore
from jiuwen_memory.storage.watchdog_impl.local_watchdog import LocalWatchdog
from tests.support.whitebox import private

pytestmark = pytest.mark.unit


def _unit(uid="e1", space="project-a"):
    return MemoryUnit(
        id=uid, scope=Scope(org="acme", space=space),
        segments=[Segment(content="Run CI after fixing config")],
        system_metadata={"memory_type": "experience"},
    )


def test_new_experience_and_metadata_are_written_in_experience_directory(tmp_path):
    store = LocalMarkdownStore(root=str(tmp_path))
    unit = _unit()
    store.write(unit.scope, [unit])
    path = "memory/experience/EXPERIENCE.md"
    assert unit.system_metadata[MD_FILENAME_KEY] == path
    absolute = tmp_path / path
    assert (tmp_path / "memory/experience/.EXPERIENCE.meta.json").is_file()
    parsed = experience_blocks.read_document(path, absolute, absolute.read_text(encoding="utf-8"))
    assert parsed["e1"].scope == unit.scope
    assert parsed["e1"].content == unit.content
    changed = replace(unit, segments=[Segment(content="CI passes")])
    store.change_experience(unit.scope, unit, changed)
    assert "CI passes" in (tmp_path / path).read_text(encoding="utf-8")
    store.change_experience(unit.scope, changed, None)
    assert (tmp_path / path).read_text(encoding="utf-8") == ""


def test_experience_file_preserves_scope_isolation(tmp_path):
    store = LocalMarkdownStore(root=str(tmp_path))
    unit = _unit()
    store.write(unit.scope, [unit])
    document = tmp_path / "memory/experience/EXPERIENCE.md"
    before = document.read_bytes()
    other = _unit("other", "project-b")
    with pytest.raises(ValidationError, match="different scope"):
        store.write(other.scope, [other])
    assert document.read_bytes() == before


@pytest.mark.parametrize("path", ["memory/EXPERIENCE.md", "memory/experience/x70/EXPERIENCE.md"])
def test_experience_document_rejects_a_noncanonical_path(tmp_path, path):
    store = LocalMarkdownStore(root=str(tmp_path))
    unit = _unit()
    unit.system_metadata[MD_FILENAME_KEY] = path
    absolute = tmp_path / path
    with pytest.raises(ValueError, match="Invalid experience document path"):
        experience_blocks.read_document(path, absolute, "")
    with pytest.raises(ValidationError, match="Invalid experience document path"):
        store.change_experience(unit.scope, unit, unit)
    assert not (tmp_path / "memory/experience/EXPERIENCE.md").exists()


def test_startup_scan_only_schedules_canonical_experience_document(tmp_path):
    canonical = "memory/experience/EXPERIENCE.md"
    for relative in (canonical, "memory/EXPERIENCE.md", "memory/experience/x70/EXPERIENCE.md"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    scheduled = Mock()
    watchdog = SimpleNamespace(
        _closed=False, _watcher_initialized=False, _markdown_root=str(tmp_path),
        _schedule_sync=scheduled,
    )
    private(LocalWatchdog, "_set_watcher_initialized")(watchdog)
    scheduled.assert_called_once()
    assert scheduled.call_args.args[0].replace("\\", "/") == (tmp_path / canonical).as_posix()


@pytest.mark.parametrize("path", ["memory/EXPERIENCE.md", "memory/experience/x70/EXPERIENCE.md"])
@pytest.mark.parametrize("deleted", [False, True])
def test_watchdog_ignores_events_for_noncanonical_experience_document(tmp_path, path, deleted):
    loop = Mock()
    watchdog = LocalWatchdog(Mock(), str(tmp_path), Scope(), loop=loop)
    watchdog.schedule_sync(str(tmp_path / path), deleted=deleted)
    loop.call_soon_threadsafe.assert_not_called()


def test_root_document_rejects_mixed_scope_metadata(tmp_path):
    path = "memory/experience/EXPERIENCE.md"
    document = tmp_path / path
    document.parent.mkdir(parents=True)
    experience_blocks.write_document(document, "", [], [_unit(), _unit("e2", "project-b")])
    with pytest.raises(ValueError, match="scope"):
        experience_blocks.read_document(path, document, document.read_text(encoding="utf-8"))
