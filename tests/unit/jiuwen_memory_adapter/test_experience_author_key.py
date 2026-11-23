from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwen_memory.api import ValidationError
from jiuwen_memory_adapter.jiuwenswarm.experience_author_key import configure_experience_author_key


def _config(key=""):
    return {"appraiser": {"experience": {"target": "experience", "params": {"author_key": key}}}}


def _configure(config):
    configure_experience_author_key(config)
    return config["appraiser"]["experience"]["params"]["author_key"]


def test_concurrent_sdk_startups_share_one_saved_author_key(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JIUWEN_MEMORY_EXPERIENCE_AUTHOR_KEY", "  ")

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = set(pool.map(_configure, (_config() for _ in range(8))))

    key_file = tmp_path / "config" / ".experience-author.key"
    saved = key_file.read_text(encoding="ascii").strip()
    assert keys == {saved}
    assert len(bytes.fromhex(saved)) == 32
    assert list(key_file.parent.iterdir()) == [key_file]


def test_explicit_key_and_environment_take_precedence_over_local_file(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JIUWEN_MEMORY_EXPERIENCE_AUTHOR_KEY", "env-test-key")
    assert _configure(_config("configured-test-key")) == "configured-test-key"
    assert _configure(_config()) == "env-test-key"
    assert not (tmp_path / "config").exists()


def test_invalid_key_file_is_not_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("JIUWEN_MEMORY_EXPERIENCE_AUTHOR_KEY", raising=False)
    key_file = tmp_path / "config" / ".experience-author.key"
    key_file.parent.mkdir()
    key_file.write_text("invalid", encoding="ascii")

    with pytest.raises(ValidationError, match="key file is invalid"):
        _configure(_config())
    assert key_file.read_text(encoding="ascii") == "invalid"
