"""自动定位配置库，并优先复用已确定的文档根目录。"""

from jiuwen_memory.config.auto_overlay import open_config_overlay, open_document_overlay


def test_document_root_automatically_persists_overlay(tmp_path):
    root = tmp_path / "new-root"
    path = tmp_path / "server.yml"
    path.write_text("{}", encoding="utf-8")
    config = {
        "globals": {"write_document": True},
        "markdown_store": {"default": {"target": "local", "params": {"root": str(root)}}},
    }
    store = open_config_overlay(config, (str(path),))
    store.commit(0, {"prompts.extract.a": "saved"})
    assert open_document_overlay(config).read().values == {"prompts.extract.a": "saved"}
    assert not list(tmp_path.glob(".server.yml.portal-*.db"))


def test_unknown_or_unsafe_locations_do_not_guess_path(tmp_path):
    assert open_document_overlay(None) is None
    base = {"globals": {"write_document": True}, "markdown_store": {"default": {
        "target": "local", "params": {"root": "relative"},
    }}}
    assert open_document_overlay(base) is None
    base["markdown_store"]["default"]["params"]["root"] = str(tmp_path)
    base["config_source"] = {"default": "dict"}
    assert open_document_overlay(base) is None
    base["config_source"] = {"default": "yaml_defaults"}
    base["globals"]["storage_encryption_enabled"] = True
    assert open_document_overlay(base) is None


def test_first_creation_failure_degrades_to_read_only(tmp_path, monkeypatch):
    from jiuwen_memory.config.overlay_store import SQLiteOverlayStore

    config = {
        "globals": {"write_document": True},
        "markdown_store": {"default": {"target": "local", "params": {"root": str(tmp_path)}}},
    }

    def deny(*args, **kwargs):
        raise PermissionError("storage unavailable")

    monkeypatch.setattr(SQLiteOverlayStore, "__init__", deny)
    assert open_document_overlay(config) is None


def test_existing_overlay_failure_does_not_silently_discard_saved_state(tmp_path, monkeypatch):
    import pytest

    from jiuwen_memory.common.errors import BackendError
    from jiuwen_memory.config.overlay_store import SQLiteOverlayStore

    config = {
        "globals": {"write_document": True},
        "markdown_store": {"default": {"target": "local", "params": {"root": str(tmp_path)}}},
    }
    open_document_overlay(config).commit(0, {"prompts.extract.a": "saved"})

    def deny(*args, **kwargs):
        raise PermissionError("storage unavailable")

    monkeypatch.setattr(SQLiteOverlayStore, "__init__", deny)
    with pytest.raises(BackendError):
        open_document_overlay(config)


def test_corrupt_existing_overlay_fails_closed(tmp_path):
    import pytest

    from jiuwen_memory.common.errors import BackendError

    config = {
        "globals": {"write_document": True},
        "markdown_store": {"default": {"target": "local", "params": {"root": str(tmp_path)}}},
    }
    internal = tmp_path / "memory" / ".shadow"
    internal.mkdir(parents=True)
    path = internal / "portal-config.db"
    path.write_text("not a sqlite database")
    path.chmod(0o600)
    with pytest.raises(BackendError):
        open_document_overlay(config)


def test_config_file_first_creation_failure_degrades_to_read_only(tmp_path, monkeypatch):
    from jiuwen_memory.config.auto_overlay import open_file_overlay
    from jiuwen_memory.config.overlay_store import SQLiteOverlayStore

    path = tmp_path / "server.yml"
    path.write_text("{}")

    def deny(*args, **kwargs):
        raise PermissionError("storage unavailable")

    monkeypatch.setattr(SQLiteOverlayStore, "__init__", deny)
    assert open_file_overlay(None, [str(path)]) is None



def test_config_file_overlay_is_stable_and_separates_config_layers(tmp_path):
    from jiuwen_memory.config.auto_overlay import open_file_overlay

    first = tmp_path / "first.yml"
    second = tmp_path / "server.yml"
    first.write_text("{}")
    second.write_text("{}")
    single = open_file_overlay(None, [str(second)])
    single.commit(0, {"llm.default.params.llm_model": "saved"})
    assert open_file_overlay(None, [str(second)]).read().values == {
        "llm.default.params.llm_model": "saved",
    }
    assert open_file_overlay(None, [str(first), str(second)]).read().version == 0
    encrypted = {"globals": {"storage_encryption_enabled": True}}
    assert open_file_overlay(encrypted, [str(second)]) is None
    assert open_file_overlay({"config_source": {"default": "dict"}}, [str(second)]) is None
    assert open_file_overlay(None, []) is None
