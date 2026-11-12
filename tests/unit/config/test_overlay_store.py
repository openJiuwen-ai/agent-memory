"""验证配置覆盖的版本化事务及重启恢复。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from jiuwen_memory.common.errors import ConflictError, ValidationError
from jiuwen_memory.config.overlay_store import SQLiteOverlayStore

pytestmark = pytest.mark.unit


def test_commit_survives_reopen_and_removes_deleted_overrides(tmp_path):
    path = tmp_path / "config.db"
    store = SQLiteOverlayStore(path)
    assert store.read().version == 0
    saved = store.commit(0, {"llm.default.params.llm_model": "model", "prompts.extract.a": "文本"})
    assert saved.version == 1
    assert SQLiteOverlayStore(path).read() == saved
    saved = store.commit(1, {"prompts.extract.a": "新文本"})
    assert SQLiteOverlayStore(path).read() == saved


def test_same_version_competing_writers_only_one_commits(tmp_path):
    path = tmp_path / "config.db"
    store = SQLiteOverlayStore(path)
    barrier = Barrier(2)

    def write(value):
        writer = SQLiteOverlayStore(path)
        barrier.wait(timeout=5)
        try:
            return writer.commit(0, {"prompts.extract.a": value})
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, ["first", "second"]))
    assert sum(result is not None for result in outcomes) == 1
    assert store.read().version == 1
    assert store.read().values["prompts.extract.a"] in {"first", "second"}


def test_invalid_write_does_not_change_saved_snapshot(tmp_path):
    store = SQLiteOverlayStore(tmp_path / "config.db")
    saved = store.commit(0, {"prompts.extract.a": "original"})
    with pytest.raises(ValidationError):
        store.commit(1, {"prompts.extract.a": float("nan")})
    with pytest.raises(ConflictError):
        store.commit(0, {"prompts.extract.a": "stale"})
    assert store.read() == saved


def test_memory_backend_is_not_mistaken_for_persistence():
    with pytest.raises(ValidationError):
        SQLiteOverlayStore(":memory:")




def test_database_failure_rolls_back_previous_state(tmp_path):
    import sqlite3

    path = tmp_path / "config.db"
    store = SQLiteOverlayStore(path)
    saved = store.commit(0, {"prompts.extract.a": "original"})
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER reject_overlay BEFORE UPDATE ON config_overlay "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.commit(1, {"prompts.extract.a": "not saved"})
    assert store.read() == saved


def test_new_database_is_private(tmp_path):
    import os
    import stat

    path = tmp_path / "config.db"
    SQLiteOverlayStore(path)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_unsafe_file_and_symlink_are_rejected(tmp_path):
    import os

    path = tmp_path / "unsafe.db"
    path.touch(mode=0o644)
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(ValidationError):
            SQLiteOverlayStore(path)
    target = tmp_path / "target.db"
    target.touch(mode=0o600)
    link = tmp_path / "link.db"
    link.symlink_to(target)
    with pytest.raises(ValidationError):
        SQLiteOverlayStore(link)
