# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""KV list 排序：naive/aware/None t_ingest 混用时不因 datetime 比较而 500。"""

from datetime import UTC, datetime

from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment, Temporal, memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore
from jiuwen_memory.storage.kv_impl.memory_list import list_memory_entries

SCOPE = Scope(org="o", space="s", user="alice")


def _entry(uid: str, t_ingest) -> tuple[str, bytes]:
    unit = MemoryUnit(
        id=uid,
        scope=SCOPE,
        segments=[Segment(content=uid)],
        temporal=Temporal(t_ingest=t_ingest),
    )
    return memory_key(uid), dumps(unit)


def _list(entries):
    return list_memory_entries(
        entries, SCOPE, offset=0, limit=10, memory_types=None, filters=None, extensions=None
    )


def test_mixed_naive_and_aware_timestamps_sort_desc() -> None:
    result = _list(
        [
            _entry("aware", datetime(2026, 7, 2, tzinfo=UTC)),
            _entry("naive", datetime(2026, 7, 3)),
        ]
    )
    assert result.count == 2
    assert [key for key, _ in result.entries] == [memory_key("naive"), memory_key("aware")]


def test_naive_only_and_none_timestamps_still_sort() -> None:
    result = _list(
        [
            _entry("old", datetime(2026, 7, 1)),
            _entry("new", datetime(2026, 7, 4)),
            _entry("missing", None),
        ]
    )
    assert [key for key, _ in result.entries] == [
        memory_key("new"),
        memory_key("old"),
        memory_key("missing"),
    ]


def test_in_memory_store_list_mixed_timestamps() -> None:
    store = InMemoryKVStore()
    store.insert(SCOPE, *_entry("aware", datetime(2026, 7, 2, tzinfo=UTC)))
    store.insert(SCOPE, *_entry("naive", datetime(2026, 7, 3)))
    result = store.list(SCOPE, offset=0, limit=10)
    assert result.count == 2
    assert [key for key, _ in result.entries] == [memory_key("naive"), memory_key("aware")]
