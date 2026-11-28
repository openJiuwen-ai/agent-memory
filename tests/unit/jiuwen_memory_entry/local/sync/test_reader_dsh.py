"""DSH session identity comes from the file and the turn inside it (F17 D15)."""

from __future__ import annotations

import json
import sys
import types

import pytest

from jiuwen_memory_entry.local.sync.access import SourceAccess
from jiuwen_memory_entry.local.sync.grant import new_grant
from jiuwen_memory_entry.local.sync.readers.dsh import DshReader

pytestmark = pytest.mark.unit


def _packer(monkeypatch):
    """Real zstd when installed, else an identity codec: identity is what we test."""
    try:
        import zstandard

        return lambda text: zstandard.ZstdCompressor().compress(text)
    except ImportError:
        pass

    class _Decompressor:
        @staticmethod
        def decompress(data, max_output_size=None):
            return data

    fake = types.ModuleType("zstandard")
    fake.ZstdDecompressor = _Decompressor
    monkeypatch.setitem(sys.modules, "zstandard", fake)
    return lambda text: text


def _session(root, name: str, turns: int, pack):
    path = root / "sessions" / "proj" / f"session-{name}" / "session.v4.jsonl.zstd"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index in range(turns):
        lines.append(
            json.dumps(
                {"type": "user/message", "payload": {"text": f"q{index}", "source": "user"}}
            )
        )
        lines.append(json.dumps({"type": "assistant/message", "payload": {"text": f"a{index}"}}))
        lines.append(json.dumps({"type": "turn/end"}))
    path.write_bytes(pack("\n".join(lines).encode()))


def _scan(root):
    grant = new_grant([str(root)], types={"session_turn"}, target_space="space")
    return DshReader().scan(
        SourceAccess(grant), types={"session_turn"}, since=None, limit=None
    )


def test_two_sessions_keep_separate_identity(tmp_path, monkeypatch):
    pack = _packer(monkeypatch)
    _session(tmp_path, "s1", 2, pack)
    _session(tmp_path, "s2", 1, pack)
    ids = [str(item.item_id) for item in _scan(tmp_path).items]
    assert ids == [
        "session_turn|sessions/proj/session-s1/session.v4.jsonl.zstd|turn:0",
        "session_turn|sessions/proj/session-s1/session.v4.jsonl.zstd|turn:1",
        "session_turn|sessions/proj/session-s2/session.v4.jsonl.zstd|turn:0",
    ]


def test_shrinking_one_session_does_not_renumber_another(tmp_path, monkeypatch):
    pack = _packer(monkeypatch)
    _session(tmp_path, "s1", 2, pack)
    _session(tmp_path, "s2", 2, pack)
    before = {str(item.item_id) for item in _scan(tmp_path).items if "s2/" in str(item.item_id)}
    _session(tmp_path, "s1", 1, pack)
    after = {str(item.item_id) for item in _scan(tmp_path).items if "s2/" in str(item.item_id)}
    assert after == before, "another session's turns must keep their identity"
