"""storage/kv.py 共享读 helper：点读 ``load_units`` 与列表读 ``list_units``。

两个 helper 是 KV 端口的跨层共用件（Engine 点读/全量扫描、Lifecycle sweep、
EvolveJob/MiddleToLongJob 候选拉取、list_page 分页）——契约：缺失省略、
保序、不去重（点读侧）；列表侧只做 ``loads`` 反序列化，过滤/计数/分页语义
全部由 ``KVStore.list`` 契约承担。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.type_def import FilterClause, FilterOp, MemoryUnit, Scope
from jiuwen_memory.common.type_def.memory import memory_key
from jiuwen_memory.common.type_def.memory_codec import dumps
from jiuwen_memory.storage.kv import list_units, load_units
from jiuwen_memory.storage.kv_impl.in_memory_kv_store import InMemoryKVStore

pytestmark = pytest.mark.unit


def _unit(uid: str, scope: Scope, content: str, *, tags: list[str] | None = None) -> MemoryUnit:
    from jiuwen_memory.common.type_def import Segment

    return MemoryUnit(
        id=uid, scope=scope, segments=[Segment(content=content)], tags=list(tags or [])
    )


def _seed(kv: InMemoryKVStore, scope: Scope) -> None:
    kv.insert(scope, memory_key("u1"), dumps(_unit("u1", scope, "one")))
    kv.insert(scope, memory_key("u2"), dumps(_unit("u2", scope, "two", tags=["t2"])))
    kv.insert(scope, memory_key("u3"), dumps(_unit("u3", scope, "three", tags=["t3"])))


# ---- list_units ----


def test_list_units_returns_deserialized_items_and_total_count() -> None:
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()
    _seed(kv, scope)

    items, count = list_units(kv, scope, limit=2, offset=1)

    assert count == 3, "count 应为分页前匹配总数，不受 offset/limit 影响"
    assert len(items) == 2, "只反序列化当前页（3 条中跳过 1 条取 2 条）"


def test_list_units_skips_non_memory_unit_records() -> None:
    """loads 对非 dict 的合法 JSON 记录返回 None，自然过滤。"""
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()
    _seed(kv, scope)
    kv.insert(scope, "/memory/not-a-unit", b"[1, 2, 3]")

    items, count = list_units(kv, scope, limit=100)

    assert {unit.id for unit in items} == {"u1", "u2", "u3"}, "非 MemoryUnit 记录被过滤"
    assert count == 3, "count 与 KVStore.list 对齐：非 MemoryUnit 记录不计入匹配总数"


def test_list_units_passes_memory_types_and_filters_through() -> None:
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()
    _seed(kv, scope)

    by_type, count = list_units(
        kv,
        scope,
        limit=100,
        memory_types=["semantic"],
    )
    assert count == 0, "memory_types 下推由 KVStore.list 承担，helper 不做二次过滤"
    assert by_type == []

    by_tag, count = list_units(
        kv,
        scope,
        limit=100,
        filters=FilterClause(field="tags", op=FilterOp.CONTAINS, value="t2"),
    )
    assert count == 1
    assert {unit.id for unit in by_tag} == {"u2"}


def test_list_units_does_not_cross_scope() -> None:
    scope = Scope(org="acme", user="u1")
    other = Scope(org="acme", user="u2")
    kv = InMemoryKVStore()
    _seed(kv, scope)

    items, count = list_units(kv, other, limit=100)

    assert items == []
    assert count == 0


# ---- load_units ----


def test_load_units_returns_hits_in_input_order_and_omits_missing() -> None:
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()
    _seed(kv, scope)

    units = load_units(kv, scope, ["u3", "missing", "u1"])

    assert [unit.id for unit in units] == ["u3", "u1"], "按输入顺序返回，缺失省略"


def test_load_units_supports_duplicate_ids_and_empty_input() -> None:
    scope = Scope(org="acme", user="u1")
    kv = InMemoryKVStore()
    _seed(kv, scope)

    assert [unit.id for unit in load_units(kv, scope, ["u1", "u1"])] == ["u1", "u1"], (
        "重复 id 各自返回，不去重"
    )
    assert load_units(kv, scope, []) == [], "空入参直接返回空列表"
