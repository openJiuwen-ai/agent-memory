"""SQLite 影子索引（``SqliteDocumentShadowIndex``）——静态派生与降级模式 CRUD/召回。

影子索引是文档模式的机器真源：memory_unit（全量）+ memory_fts（FTS5 倒排）+ memory_vec
（vec0 向量，仅完整模式），三表同库靠隐式 rowid 关联。本文件不依赖 sqlite-vec / embedder，
全部走降级模式（embedder=None）——两表 + 倒排照常，向量路返空。失效方向：

- ``get_units`` 缺失 id 省略（与 KV ``mget`` 抛 NotFoundError 刻意不同）：召回物化侧
  命中的 unit_id 中途被删应静默跳过，而非整批失败。
- update 的「空兜底守卫」：coords 是 TRANSIENT 键（dumps 剥除），read-modify-write 的
  unit 读回后 project 落 default；不守卫会重写归属、下次按 project 隔离召回丢失。
- 系统谓词（lifecycle/t_valid/t_invalid/t_event）编译下推：OR 组含无约束 child 须整体
  放弃（恒真不可下推），点读后复核兜底。
"""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为

import hashlib
import struct
from datetime import datetime, timezone

import pytest

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import (
    WhitespaceTokenizer,
)
from jiuwen_memory.common.type_def import (
    COORDS_KEY,
    MD_FILENAME_KEY,
    MEMORY_CLASS_KEY,
    T_EVENT_UNKNOWN,
    T_INVALID_OPEN,
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    MemoryUnit,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import (
    SqliteDocumentShadowIndex,
    _build_schema,
    _compile_clause,
    _compile_system_filters,
    _epoch_ms,
    _lifecycle_of,
    _t_event_of,
    _t_invalid_of,
    _t_valid_of,
    _vec_to_blob,
)
from jiuwen_memory.storage.types import ScoredID, TextQuery

# 这几个是 ``SqliteDocumentShadowIndex`` 的 @staticmethod（非模块级函数），
# 以别名暴露，测试体调用保持简洁（与上方模块级纯函数区分开）。
_content_of = SqliteDocumentShadowIndex._content_of
_content_hash = SqliteDocumentShadowIndex._content_hash
_project_of = SqliteDocumentShadowIndex._project_of
_projects_from_filters = SqliteDocumentShadowIndex._projects_from_filters
_category_of = SqliteDocumentShadowIndex._category_of
_md_filename_of = SqliteDocumentShadowIndex._md_filename_of

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")


def _store(tmp_path, tokenizer=None) -> SqliteDocumentShadowIndex:
    return SqliteDocumentShadowIndex(
        db_path=str(tmp_path / "shadow.db"),
        tokenizer=tokenizer or WhitespaceTokenizer(),
    )


def _unit(uid: str, content: str, metadata: dict | None = None) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=SCOPE,
        segments=[Segment(content=content)],
        system_metadata=dict(metadata or {}),
    )


# -- 静态派生 ---------------------------------------------------------------- #


def test_content_of_reads_first_segment() -> None:
    assert _content_of(_unit("u1", "hello")) == "hello"
    assert _content_of(MemoryUnit(id="empty", scope=SCOPE, segments=[])) == ""


def test_content_hash_is_sha256_of_raw_content() -> None:
    assert _content_hash("hello") == hashlib.sha256(b"hello").hexdigest()
    assert _content_hash("hello") != _content_hash("hello ")


def test_project_of_reads_coords_or_defaults() -> None:
    assert _project_of(_unit("u1", "x", {COORDS_KEY: {"project": "p1"}})) == "p1"
    assert _project_of(_unit("u1", "x", {})) == "default"


def test_category_of_reads_memory_class_or_defaults() -> None:
    assert _category_of(_unit("u1", "x", {MEMORY_CLASS_KEY: "project_memory"})) == "project_memory"
    assert _category_of(_unit("u1", "x", {})) == "team_memory"


def test_md_filename_of_reads_backfilled_path() -> None:
    unit = _unit("u1", "x", {MD_FILENAME_KEY: "memory/p1/MEMORY.md"})
    assert _md_filename_of(unit) == "memory/p1/MEMORY.md"
    assert _md_filename_of(_unit("u1", "x", {})) == ""


# -- 时间/生命周期投影 ------------------------------------------------------- #


def test_epoch_ms_converts_datetime_and_none() -> None:
    dt = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert _epoch_ms(dt) == int(dt.timestamp() * 1000)
    assert _epoch_ms(None) is None


def test_temporal_projection_uses_sentinels_for_open_ends() -> None:
    dt = datetime(2026, 9, 1, tzinfo=timezone.utc)
    unit = MemoryUnit(id="u1", scope=SCOPE, temporal=Temporal(t_valid=dt))
    assert _t_valid_of(unit) == int(dt.timestamp() * 1000)
    assert _t_invalid_of(unit) == T_INVALID_OPEN  # t_invalid=None → 开放哨兵
    assert _t_event_of(unit) == T_EVENT_UNKNOWN  # t_event=None → 未知哨兵
    assert _lifecycle_of(unit) == "active"


# -- 系统谓词编译 ------------------------------------------------------------ #


def test_compile_clause_covers_all_operators() -> None:
    assert _compile_clause("lifecycle", FilterOp.EQ, "active") == ("lifecycle = ?", ["active"])
    assert _compile_clause("lifecycle", FilterOp.NE, "active") == ("lifecycle != ?", ["active"])
    assert _compile_clause("t_event", FilterOp.IN, [1, 2]) == ("t_event IN (?,?)", [1, 2])
    assert _compile_clause("t_event", FilterOp.NOT_IN, [1]) == ("t_event NOT IN (?)", [1])
    assert _compile_clause("t_valid", FilterOp.GT, 5) == ("t_valid > ?", [5])
    assert _compile_clause("t_valid", FilterOp.GTE, 5) == ("t_valid >= ?", [5])
    assert _compile_clause("t_valid", FilterOp.LT, 5) == ("t_valid < ?", [5])
    assert _compile_clause("t_valid", FilterOp.LTE, 5) == ("t_valid <= ?", [5])
    # CONTAINS 对系统字段无意义 → 不可编译
    assert _compile_clause("lifecycle", FilterOp.CONTAINS, "x") == (None, [])


def test_compile_system_filters_drops_non_system_fields() -> None:
    """非系统字段（如 project）叶子返回无约束，由点读后复核兜底。"""
    sql, params = _compile_system_filters(FilterClause("system_metadata.project", FilterOp.EQ, "p1"))
    assert sql is None
    assert params == []


def test_compile_system_filters_and_skips_unconstrained_children() -> None:
    group = FilterGroup(
        FilterLogic.AND,
        [
            FilterClause("lifecycle", FilterOp.EQ, "active"),
            FilterClause("system_metadata.project", FilterOp.EQ, "p1"),  # 无约束
            FilterClause("t_event", FilterOp.GT, 0),
        ],
    )
    sql, params = _compile_system_filters(group)
    assert sql == "(lifecycle = ?) AND (t_event > ?)"
    assert params == ["active", 0]


def test_compile_system_filters_or_with_unconstrained_child_is_abandoned() -> None:
    """OR 组含无约束 child → 整体恒真，不可下推（放弃整组）。"""
    group = FilterGroup(
        FilterLogic.OR,
        [
            FilterClause("lifecycle", FilterOp.EQ, "active"),
            FilterClause("system_metadata.project", FilterOp.EQ, "p1"),
        ],
    )
    assert _compile_system_filters(group) == (None, [])


# -- 建表 DDL 与向量编码 ----------------------------------------------------- #


def test_build_schema_skips_vec_table_in_degraded_mode() -> None:
    ddl = _build_schema(None)
    assert "memory_unit" in ddl
    assert "memory_fts" in ddl
    assert "memory_vec" not in ddl


def test_build_schema_includes_vec_table_in_full_mode() -> None:
    ddl = _build_schema(64)
    assert "memory_vec" in ddl
    assert "float[64]" in ddl


def test_vec_to_blob_is_little_endian_float32() -> None:
    vec = [1.0, -2.5, 3.0]
    assert _vec_to_blob(vec) == struct.pack("<3f", 1.0, -2.5, 3.0)


# -- projects 从 filters 收集 ------------------------------------------------- #


def test_projects_from_filters_collects_and_dedups() -> None:
    filters = FilterGroup(
        FilterLogic.AND,
        [
            FilterClause("system_metadata.project", FilterOp.IN, ["p1", "p2"]),
            FilterClause("system_metadata.project", FilterOp.IN, ["p2", ""]),
        ],
    )
    # 空串是「该维不适用」兜底项，过滤掉
    assert _projects_from_filters(filters) == ["p1", "p2"]


def test_projects_from_filters_defaults_when_no_project_predicate() -> None:
    assert _projects_from_filters(None) == ["default"]
    assert _projects_from_filters(FilterClause("user_metadata.x", FilterOp.EQ, "v")) == ["default"]


# -- 降级模式 CRUD 往返 ------------------------------------------------------ #


def test_insert_and_get_roundtrip(tmp_path) -> None:
    store = _store(tmp_path)
    unit = _unit("u1", "hello world", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}})

    store.insert_units(SCOPE, [unit])

    got = store.get_units(SCOPE, ["u1"])
    assert [u.id for u in got] == ["u1"]
    assert got[0].segments[0].content == "hello world"


def test_get_units_omits_missing_ids_and_preserves_order(tmp_path) -> None:
    store = _store(tmp_path)
    store.insert_units(SCOPE, [_unit("u1", "a"), _unit("u2", "b")])

    got = store.get_units(SCOPE, ["u2", "missing", "u1"])
    assert [u.id for u in got] == ["u2", "u1"]


def test_insert_duplicate_id_conflicts(tmp_path) -> None:
    store = _store(tmp_path)
    store.insert_units(SCOPE, [_unit("u1", "a")])
    with pytest.raises(ConflictError):
        store.insert_units(SCOPE, [_unit("u1", "b")])


def test_update_overwrites_content_and_preserves_project_guard(tmp_path) -> None:
    """coords 是 TRANSIENT 键（dumps 剥除），read-modify-write 后 project 落 default；
    空兜底守卫须保留旧 project，否则按 project 隔离召回丢失。
    """
    store = _store(tmp_path)
    store.insert_units(
        SCOPE,
        [_unit("u1", "hello world", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}})],
    )
    # 读回（无 coords），改 content，再 update
    (read_back,) = store.get_units(SCOPE, ["u1"])
    read_back.segments[0].content = "updated content"
    read_back.system_metadata = dict(read_back.system_metadata)  # 已无 coords

    store.update_units(SCOPE, [read_back])

    assert store.get_units(SCOPE, ["u1"])[0].segments[0].content == "updated content"
    # project 仍为 p1（非 default）：按 project 过滤召回应命中
    hits = store.search_fulltext(SCOPE, TextQuery(text="updated", 
            filters=FilterClause("system_metadata.project", FilterOp.IN, ["p1"])))
    assert [h.id for h in hits] == ["u1"]


def test_update_missing_id_raises_not_found(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(NotFoundError):
        store.update_units(SCOPE, [_unit("nope", "x")])


def test_update_state_only_keeps_content(tmp_path) -> None:
    """content 不变（只改状态字段）→ 只覆写 unit_json，不重建投影。"""
    from jiuwen_memory.common.type_def import LifecycleState

    store = _store(tmp_path)
    store.insert_units(SCOPE, [_unit("u1", "hello world")])
    (read_back,) = store.get_units(SCOPE, ["u1"])
    read_back.lifecycle = LifecycleState.SUPERSEDED  # content 不变

    store.update_units(SCOPE, [read_back])

    got = store.get_units(SCOPE, ["u1"])[0]
    assert got.lifecycle is LifecycleState.SUPERSEDED
    assert got.segments[0].content == "hello world"


def test_delete_is_idempotent(tmp_path) -> None:
    store = _store(tmp_path)
    store.insert_units(SCOPE, [_unit("u1", "a"), _unit("u2", "b")])

    store.delete_units(SCOPE, ["u1", "missing", "u1"])
    assert [u.id for u in store.get_units(SCOPE, ["u1", "u2"])] == ["u2"]


def test_list_units_and_list_by_md(tmp_path) -> None:
    store = _store(tmp_path)
    md = "memory/p1/MEMORY.md"
    store.insert_units(
        SCOPE,
        [
            _unit("u1", "hello", {MD_FILENAME_KEY: md}),
            _unit("u2", "world", {MD_FILENAME_KEY: md}),
            _unit("u3", "other", {MD_FILENAME_KEY: "memory/else.md"}),
        ],
    )

    assert [uid for uid, _ in store.list_units(SCOPE)] == ["u1", "u2", "u3"]
    by_md = store.list_units_by_md(SCOPE, md)
    assert {uid for uid, _ in by_md} == {"u1", "u2"}
    assert all(ch == _content_hash(c) for ch, c in zip((x[1] for x in by_md), ("hello", "world")))


# -- 召回（降级模式） -------------------------------------------------------- #


def test_search_fulltext_returns_ranked_hits(tmp_path) -> None:
    store = _store(tmp_path)
    store.insert_units(
        SCOPE,
        [
            _unit("u1", "deploy cluster", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}),
            _unit("u2", "cluster failure", {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}),
            _unit("u3", "coffee preference", {MEMORY_CLASS_KEY: "user_memory"}),
        ],
    )

    hits = store.search_fulltext(
        SCOPE,
        TextQuery(
            text="cluster",
            top_k=10,
            filters=FilterClause("system_metadata.project", FilterOp.IN, ["p1"]),
        ),
    )

    assert {h.id for h in hits} == {"u1", "u2"}
    assert all(isinstance(h, ScoredID) for h in hits)


def test_search_fulltext_recalls_when_query_has_unseen_token(tmp_path) -> None:
    """OR 连接：查询含文档没有的词（疑问词/停用词）不应让整条查询落空。

    FTS5 空格分隔是隐式 AND——「张三 喜欢 什么 咖啡」里的「什么」不在任何文档
    时整条 0 命中。MATCH 串改 ``" OR "`` 连接后，任一词命中即进候选，多词同命中
    靠 bm25 排到前面（回归锁，防改回 AND）。
    """
    store = _store(tmp_path)
    store.insert_units(
        SCOPE,
        [
            _unit("u1", "张三喜欢喝拿铁咖啡"),
            _unit("u2", "李四的工位在B区3楼"),
        ],
    )

    # 查询分出「什么」，两篇文档都没有——OR 语义下仍应召回 u1。
    hits = store.search_fulltext(SCOPE, TextQuery(text="张三 喜欢 什么 咖啡", top_k=5))

    assert {h.id for h in hits} == {"u1"}


def test_search_fulltext_returns_empty_for_empty_tokens(tmp_path) -> None:
    store = _store(tmp_path)
    store.insert_units(SCOPE, [_unit("u1", "hello world")])
    assert store.search_fulltext(SCOPE, TextQuery(text="   ")) == []


def test_search_vector_is_empty_in_degraded_mode(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.vec_enabled is False
    from jiuwen_memory.storage.types import VectorQuery

    assert store.search_vector(SCOPE, VectorQuery(vector=[0.1])) == []


def test_health_and_close(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.health() is None
    store.insert_units(SCOPE, [_unit("u1", "hello")])
    store.close()
    store.close()  # 幂等
