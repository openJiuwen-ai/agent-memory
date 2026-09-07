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
from jiuwen_memory.common.type_def.filter import and_merge
from jiuwen_memory.storage.shadow import DocumentShadowIndex
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
_has_project_predicate = SqliteDocumentShadowIndex._has_project_predicate
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
    assert _project_of(_unit("u1", "x", {})) == ""  # 空串兜底（跨项目可见）


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


def test_compile_system_filters_compiles_project_field() -> None:
    """project 是系统字段，经编译路径下推（保留 AND/OR，替代 _projects_from_filters 平铺）。"""
    clause = FilterClause("system_metadata.project", FilterOp.EQ, "p1")
    sql, params = _compile_system_filters(clause)
    assert sql == "project = ?"
    assert params == ["p1"]
    # IN ["", value] 仍是一条 SQL 同时搜当前 project + 默认 project（跨项目可见）
    sql_in, params_in = _compile_system_filters(
        FilterClause("system_metadata.project", FilterOp.IN, ["", "p1"])
    )
    assert sql_in == "project IN (?,?)"
    assert params_in == ["", "p1"]


def test_compile_system_filters_drops_non_system_fields() -> None:
    """非系统字段（如 user_metadata 键）叶子返回无约束，由点读后复核兜底。"""
    sql, params = _compile_system_filters(FilterClause("user_metadata.x", FilterOp.EQ, "v"))
    assert sql is None
    assert params == []


def test_compile_system_filters_and_skips_unconstrained_children() -> None:
    group = FilterGroup(
        FilterLogic.AND,
        [
            FilterClause("lifecycle", FilterOp.EQ, "active"),
            FilterClause("system_metadata.project", FilterOp.EQ, "p1"),  # 现为系统字段，被编译
            FilterClause("t_event", FilterOp.GT, 0),
            FilterClause("user_metadata.x", FilterOp.EQ, "v"),  # 非系统字段，无约束
        ],
    )
    sql, params = _compile_system_filters(group)
    assert sql == "(lifecycle = ?) AND (project = ?) AND (t_event > ?)"
    assert params == ["active", "p1", 0]


def test_compile_system_filters_or_with_unconstrained_child_is_abandoned() -> None:
    """OR 组含无约束 child → 整体恒真，不可下推（放弃整组）。project 现为系统字段不再触发。"""
    # project + lifecycle 均为系统字段 → OR 组正常编译（不再被放弃）
    group = FilterGroup(
        FilterLogic.OR,
        [
            FilterClause("lifecycle", FilterOp.EQ, "active"),
            FilterClause("system_metadata.project", FilterOp.EQ, "p1"),
        ],
    )
    sql, params = _compile_system_filters(group)
    assert sql == "(lifecycle = ?) OR (project = ?)"
    assert params == ["active", "p1"]
    # 含真正无约束 child（user_metadata）→ 仍整体放弃
    group_abandon = FilterGroup(
        FilterLogic.OR,
        [
            FilterClause("lifecycle", FilterOp.EQ, "active"),
            FilterClause("user_metadata.x", FilterOp.EQ, "v"),  # 无约束
        ],
    )
    assert _compile_system_filters(group_abandon) == (None, [])


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
    # 空串保留：跨项目可见记忆的 project 列值，IN ('', value) 命中空串行
    assert _projects_from_filters(filters) == ["p1", "p2", ""]


def test_projects_from_filters_defaults_when_no_project_predicate() -> None:
    # 无 project 谓词 → 兜底 [""]（只召回空串行=跨项目可见记忆）
    assert _projects_from_filters(None) == [""]
    assert _projects_from_filters(FilterClause("user_metadata.x", FilterOp.EQ, "v")) == [""]


# -- project 谓词走编译路径保留 AND/OR（替代 _projects_from_filters 平铺）--------- #


def test_project_predicate_compiled_preserves_and_semantics() -> None:
    """系统收窄 project AND 用户冲突值 project → AND 在 SQL 层正确还原（不再降级 OR）。

    回归 P1：旧 ``_projects_from_filters`` 把两谓词值平铺成 ``{p1, p2}`` 丢 AND，
    导致冲突值时 p2 越权泄露。改走 ``_compile_system_filters`` 后保留 AND → 空集正确。
    """
    # 系统 narrow：project IN ["", "p1"]（space_predicates 口径）
    sys_clause = FilterClause("system_metadata.project", FilterOp.IN, ["", "p1"])
    # 用户过滤：project == "p2"（与系统冲突）
    user_clause = FilterClause("system_metadata.project", FilterOp.EQ, "p2")
    combined = and_merge(user_clause, [sys_clause])

    sql, params = _compile_system_filters(combined)
    # AND 编译：两个 project 谓词都保留，逻辑 AND（非 OR 并集）
    assert "project IN (?,?)" in sql
    assert "project = ?" in sql
    assert " AND " in sql
    assert " OR " not in sql
    assert params == ["", "p1", "p2"]  # sys 先（IN ['','p1']）、user 后（EQ 'p2'），and_merge 顺序


def test_project_predicate_compiled_preserves_or_semantics() -> None:
    """用户 OR(p1, p2) → SQL 保留 OR（不被合并成 IN 并集）。"""
    group = FilterGroup(
        FilterLogic.OR,
        [
            FilterClause("system_metadata.project", FilterOp.EQ, "p1"),
            FilterClause("system_metadata.project", FilterOp.EQ, "p2"),
        ],
    )
    sql, params = _compile_system_filters(group)
    assert sql == "(project = ?) OR (project = ?)"
    assert params == ["p1", "p2"]


def test_has_project_predicate_detects_presence() -> None:
    proj_clause = FilterClause("system_metadata.project", FilterOp.EQ, "p1")
    assert _has_project_predicate(proj_clause) is True
    assert _has_project_predicate(None) is False
    assert _has_project_predicate(FilterClause("user_metadata.x", FilterOp.EQ, "v")) is False


def test_search_fulltext_no_project_predicate_defaults_to_empty_string(tmp_path) -> None:
    """无 project 谓词 → 兜底 project=''（只召回跨项目可见空串行，不放宽成全库）。"""
    store = _store(tmp_path)
    # 空串 project（跨项目可见）+ 具体 project 各一条
    store.insert_units(SCOPE, [_unit("u-empty", "alpha", {MEMORY_CLASS_KEY: "team_memory"})])
    p1_meta = {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}
    store.insert_units(SCOPE, [_unit("u-p1", "alpha", p1_meta)])
    # 无 project 谓词召回
    hits = store.search_fulltext(SCOPE, TextQuery(text="alpha", top_k=10))
    hit_ids = [h.id for h in hits]
    assert "u-empty" in hit_ids  # 空串行命中
    assert "u-p1" not in hit_ids  # 具体 project 行不命中（兜底未放宽）


def test_search_fulltext_project_and_conflict_values_yields_empty(tmp_path) -> None:
    """系统 p1 AND 用户 p2（冲突）→ 召回空集（AND 语义正确，P1 修复回归）。"""
    store = _store(tmp_path)
    p1_meta = {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p1"}}
    p2_meta = {MEMORY_CLASS_KEY: "project_memory", COORDS_KEY: {"project": "p2"}}
    store.insert_units(SCOPE, [_unit("u-p1", "alpha", p1_meta)])
    store.insert_units(SCOPE, [_unit("u-p2", "alpha", p2_meta)])

    sys_clause = FilterClause("system_metadata.project", FilterOp.IN, ["", "p1"])
    user_clause = FilterClause("system_metadata.project", FilterOp.EQ, "p2")
    combined = and_merge(user_clause, [sys_clause])
    hits = store.search_fulltext(SCOPE, TextQuery(text="alpha", top_k=10, filters=combined))
    assert hits == []  # AND(p1, p2) 空集，p2 不泄露


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


def test_update_preserves_md_filename_in_unit_json_when_new_unit_lacks_it(tmp_path) -> None:
    """new unit 不带 MD_FILENAME_KEY 时，空兜底守卫须把旧值回填进 unit.system_metadata，
    使 dumps(unit) 序列化的 unit_json 保键——否则 get_units 读回不带键，传染性丢失。

    复现 bug B：守卫只保护投影列局部变量、未回填 system_metadata。
    """
    store = _store(tmp_path)
    md = "memory/p1/MEMORY.md"
    store.insert_units(
        SCOPE,
        [_unit("u1", "hello world", {MD_FILENAME_KEY: md, MEMORY_CLASS_KEY: "project_memory"})],
    )
    # 构造 new unit：同 id、改 content，但**不带** MD_FILENAME_KEY（模拟 evolver dedup
    # 多源交集丢键 / 上游重建对象丢键）。
    new_unit = MemoryUnit(
        id="u1",
        scope=SCOPE,
        segments=[Segment(content="updated content")],
        system_metadata={MEMORY_CLASS_KEY: "project_memory"},  # 故意无 MD_FILENAME_KEY
    )
    store.update_units(SCOPE, [new_unit])

    # ① 投影列 md_filename 保留（守卫已兜进局部变量）
    by_md = store.list_units_by_md(SCOPE, md)
    assert {uid for uid, _ in by_md} == {"u1"}
    # ② unit_json 保键：get_units 读回的 unit 仍带 MD_FILENAME_KEY（阻断传染）
    (read_back,) = store.get_units(SCOPE, ["u1"])
    assert read_back.system_metadata[MD_FILENAME_KEY] == md


def test_update_md_filename_loss_is_not_contagious_across_rounds(tmp_path) -> None:
    """两轮 update 验证传染被阻断：首轮 new unit 不带键，第二轮用读回的 unit（作为 old）
    再改 content 调 update——修复前第二轮 old 已不带键（bug B 传染），修复后仍带键。
    """
    store = _store(tmp_path)
    md = "memory/p1/MEMORY.md"
    store.insert_units(
        SCOPE,
        [_unit("u1", "first", {MD_FILENAME_KEY: md, MEMORY_CLASS_KEY: "project_memory"})],
    )
    # 第一轮：new unit 不带 MD_FILENAME_KEY
    store.update_units(
        SCOPE,
        [MemoryUnit(id="u1", scope=SCOPE, segments=[Segment(content="second")],
                    system_metadata={MEMORY_CLASS_KEY: "project_memory"})],
    )
    # 第二轮：读回第一轮结果（作为 old），改 content 再 update
    (round1,) = store.get_units(SCOPE, ["u1"])
    round1.segments[0].content = "third"
    round1.system_metadata = dict(round1.system_metadata)  # 模拟 read-modify-write
    store.update_units(SCOPE, [round1])

    (round2,) = store.get_units(SCOPE, ["u1"])
    assert round2.system_metadata[MD_FILENAME_KEY] == md
    assert round2.segments[0].content == "third"


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


# -- 契约默认值与降级可观测性 ------------------------------------------------ #


def test_document_shadow_index_vec_enabled_defaults_to_false() -> None:
    """契约（shadow.py）默认安全关闭：不覆盖 ``vec_enabled`` 的实现默认 False（降级），
    调用方据此区分「能力不可用」与「零召回」——而非靠 ``search_vector`` 空结果反推。
    """

    class _MinimalShadow(DocumentShadowIndex):

        def insert_units(self, scope, units):
            ...

        def get_units(self, scope, unit_ids):
            ...

        def update_units(self, scope, units):
            ...

        def delete_units(self, scope, unit_ids):
            ...

        def list_units(self, scope):
            ...

        def list_units_by_md(self, scope, md_filename):
            ...

        def search_fulltext(self, scope, query):
            ...

        def search_vector(self, scope, query):
            ...

        def health(self):
            ...

    assert _MinimalShadow().vec_enabled is False


def test_vec_load_failure_logs_warning(tmp_path, caplog) -> None:
    """``sqlite_vec.load`` 失败须打 warning（可观测）——否则「插件缺失」被静默吞掉，
    向量召回缺一路无从定位（建议 7 修复回归锁）。
    """
    import logging

    class _FakeVecModule:
        @staticmethod
        def load(_conn) -> None:
            raise RuntimeError("vec0 extension unavailable")

    store = _store(tmp_path)
    # 伪造「embedder 非 None + sqlite_vec 可导入但 load 失败」→ 触发降级分支
    store._embedder = object()
    store._sqlite_vec = _FakeVecModule()

    with caplog.at_level(logging.WARNING):
        store._ensure_conn()

    assert store.vec_enabled is False
    assert any("sqlite_vec load failed" in r.message for r in caplog.records)
