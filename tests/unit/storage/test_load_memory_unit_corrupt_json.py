# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Corrupt KV JSON records must be isolated, not abort a whole list/read."""

from jiuwen_memory.common.type_def import Scope, memory_key
from jiuwen_memory.storage.kv import load_memory_unit

SCOPE = Scope(org="o", space="s", user="u")


def test_malformed_json_record_returns_none() -> None:
    raw = b'{"id": "broken", "segments": ['

    assert load_memory_unit(raw, scope=SCOPE, key=memory_key("broken")) is None


def test_invalid_utf8_record_returns_none() -> None:
    raw = b'\xff\xfe\x00not utf8'

    assert load_memory_unit(raw, scope=SCOPE, key=memory_key("binary")) is None
