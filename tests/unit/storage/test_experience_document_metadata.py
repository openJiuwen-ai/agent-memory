"""Readable experience documents and their metadata file."""

from dataclasses import replace

import pytest

from jiuwen_memory.common.type_def import MD_FILENAME_KEY, MemoryUnit, Scope, Segment
from jiuwen_memory.storage.markdown_impl import experience_blocks as blocks
from jiuwen_memory.storage.markdown_impl.local_markdown_store import LocalMarkdownStore
from tests.support.whitebox import private


def _experience(unit_id, content):
    return MemoryUnit(
        id=unit_id, scope=Scope(org="acme", space="shared-experience"),
        segments=[Segment(content=content)],
        system_metadata={"memory_type": "experience", "category": "other"},
    )


def test_experience_update_preserves_metadata_and_multiline_body(tmp_path):
    store = LocalMarkdownStore(root=str(tmp_path))
    unit = _experience("first", "Original lesson")
    relative = "memory/experience/EXPERIENCE.md"
    document = tmp_path / relative
    store.write(unit.scope, [unit])

    updated = replace(unit, segments=[Segment(content="Revised lesson\n- A detail")])
    store.change_experience(unit.scope, unit, updated)
    source = document.read_text(encoding="utf-8")
    assert source == "- Revised lesson\n  - A detail\n\n"
    assert (document.parent / blocks.METADATA_NAME).is_file()
    found = blocks.read_document(relative, document, source)["first"]
    assert found.scope == unit.scope
    assert found.content == updated.content
    assert found.system_metadata["category"] == "other"


def test_markdown_text_can_be_edited_without_changing_identity(tmp_path):
    first = _experience("first", "Original")
    second = _experience("second", "Second lesson")
    store = LocalMarkdownStore(root=str(tmp_path))
    store.write(first.scope, [first, second])
    path = first.system_metadata[MD_FILENAME_KEY]
    document = tmp_path / path
    source = document.read_text(encoding="utf-8")
    assert "experience:" not in source
    modified = source.replace("Original", "Edited")
    found = blocks.read_document(path, document, modified)
    assert found["first"].content == "Edited"
    assert found["second"].content == "Second lesson"
    with pytest.raises(ValueError, match="reorder"):
        blocks.read_document(path, document, "- Second lesson\n\n- Original\n\n")


@pytest.mark.parametrize("failure_file", ["document", "metadata_cleanup"])
def test_interrupted_write_never_pairs_new_text_with_old_identity(
    tmp_path, monkeypatch, failure_file,
):
    first = _experience("first", "Original")
    second = _experience("second", "New")
    store = LocalMarkdownStore(root=str(tmp_path))
    store.write(first.scope, [first])
    path = first.system_metadata[MD_FILENAME_KEY]
    document = tmp_path / path
    original_replace = private(blocks, "_replace_text")

    def fail_if_selected(target, content):
        if failure_file == "document" and target == document:
            raise OSError("document write failed")
        if (
            failure_file == "metadata_cleanup"
            and target.name == blocks.METADATA_NAME
            and '"pending"' not in content
        ):
            raise OSError("metadata cleanup failed")
        original_replace(target, content)

    monkeypatch.setattr(blocks, "_replace_text", fail_if_selected)
    if failure_file == "document":
        with pytest.raises(OSError, match="document write failed"):
            store.write(second.scope, [second])
        expected = ["first"]
    else:
        store.write(second.scope, [second])
        expected = ["first", "second"]

    found = blocks.read_document(path, document, document.read_text(encoding="utf-8"))
    assert list(found) == expected
    assert found["first"].content == "Original"


def test_watchdog_rebuilds_from_body_and_metadata(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import Mock

    from jiuwen_memory.storage.watchdog_impl.local_watchdog import LocalWatchdog

    unit = _experience("first", "Original")
    store = LocalMarkdownStore(root=str(tmp_path))
    store.write(unit.scope, [unit])
    path = unit.system_metadata[MD_FILENAME_KEY]
    document = tmp_path / path
    document.write_text("- Edited by the user\n\n", encoding="utf-8")
    shadow = Mock()
    watchdog = SimpleNamespace(
        _markdown_root=str(tmp_path), _scope=Scope(org="fallback"), _shadow=shadow,
        _sync_gate=SimpleNamespace(
            snapshot_version=lambda: 0, commit_sync=lambda _: nullcontext(True),
        ),
    )
    assert private(LocalWatchdog, "_sync_experience")(watchdog, path, False)
    scope, filename, units = shadow.sync_experience_document.call_args.args
    assert scope == unit.scope and filename == path
    assert units["first"].content == "Edited by the user"



def test_removing_markdown_list_item_also_removes_search_hit(tmp_path):
    from types import SimpleNamespace

    from jiuwen_memory.storage.types import TextQuery
    from jiuwen_memory.storage.watchdog_impl.local_watchdog import LocalWatchdog
    from tests.unit.storage.test_sqlite_shadow_index import _store

    first = _experience("first", "apple")
    second = _experience("second", "banana")
    store = LocalMarkdownStore(root=str(tmp_path))
    store.write(first.scope, [first, second])
    path = first.system_metadata[MD_FILENAME_KEY]
    document = tmp_path / path
    shadow = _store(tmp_path)
    watchdog = SimpleNamespace(
        _markdown_root=str(tmp_path), _scope=first.scope, _shadow=shadow,
    )
    try:
        assert private(LocalWatchdog, "_sync_experience")(watchdog, path, False)
        matches = shadow.search_fulltext(first.scope, TextQuery(text="apple"))
        assert [item.id for item in matches] == ["first"]
        document.write_text("- apple\n\n- blueberry\n", encoding="utf-8")
        assert private(LocalWatchdog, "_sync_experience")(watchdog, path, False)

        document.write_text("- blueberry\n", encoding="utf-8")
        assert private(LocalWatchdog, "_sync_experience")(watchdog, path, False)
        assert not shadow.search_fulltext(first.scope, TextQuery(text="apple"))
        hits = shadow.search_fulltext(first.scope, TextQuery(text="blueberry"))
        assert [item.id for item in hits] == ["second"]
        remaining = blocks.read_document(path, document, document.read_text(encoding="utf-8"))
        assert list(remaining) == ["second"]
    finally:
        shadow.close()


def test_ambiguous_deletion_of_identical_experience_does_not_guess_id(tmp_path):
    first = _experience("first", "Same body")
    second = _experience("second", "Same body")
    LocalMarkdownStore(root=str(tmp_path)).write(first.scope, [first, second])
    path = first.system_metadata[MD_FILENAME_KEY]
    with pytest.raises(ValueError, match="identical"):
        blocks.read_document(path, tmp_path / path, "- Same body\n")
