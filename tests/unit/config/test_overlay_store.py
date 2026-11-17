"""验证配置覆盖的版本化事务及重启恢复。"""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from jiuwen_memory.common.errors import BackendError, ConflictError, ValidationError
from jiuwen_memory.common.type_def import Scope
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


def test_legacy_overlay_and_credentials_remain_owned_by_default(tmp_path):
    root = tmp_path / "Library"
    root.mkdir()
    path = root / "config.db"
    alternate_root = root.with_name("library")
    original_path = alternate_root / path.name if alternate_root.exists() else path
    legacy = SQLiteOverlayStore(original_path)
    key_field = "llm.default.params.llm_api_key"
    original_credentials = legacy.credential_store()
    legacy_identity = hashlib.sha256(str(original_path.resolve()).encode()).hexdigest()
    original_credentials.scope = Scope(org="model-config", user=legacy_identity)
    default_ref = original_credentials.put("default-key")
    saved = legacy.commit(0, {key_field: default_ref})
    with sqlite3.connect(path) as db:
        db.executescript("""
            ALTER TABLE config_overlay RENAME TO newer_overlay;
            CREATE TABLE config_overlay (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL, payload TEXT NOT NULL
            );
            INSERT INTO config_overlay SELECT 1, version, payload FROM newer_overlay;
            DROP TABLE newer_overlay;
            ALTER TABLE model_credentials RENAME TO newer_credentials;
            CREATE TABLE model_credentials (reference TEXT PRIMARY KEY, ciphertext BLOB NOT NULL);
            INSERT INTO model_credentials SELECT reference, ciphertext FROM newer_credentials;
            DROP TABLE newer_credentials;
        """)

    separate = SQLiteOverlayStore(path, instance_id="separate")
    assert separate.read().version == 0
    credentials = separate.credential_store()
    with pytest.raises(BackendError, match="unavailable"):
        credentials.get(default_ref)
    own_ref = credentials.put("separate-key")
    own_saved = separate.commit(0, {key_field: own_ref})
    credentials.retire([default_ref, own_ref])
    assert SQLiteOverlayStore(original_path, instance_id="separate").credential_store().get(
        own_ref,
    ) == "separate-key"
    assert SQLiteOverlayStore(path, instance_id="separate").read() == own_saved
    assert SQLiteOverlayStore(path).read() == saved
    assert SQLiteOverlayStore(original_path).credential_store().get(default_ref) == "default-key"
    assert SQLiteOverlayStore(path).credential_store().get(default_ref) == "default-key"


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
