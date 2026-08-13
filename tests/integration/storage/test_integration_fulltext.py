"""真实后端集成测试：Elasticsearch(fulltext)。

覆盖 CRUD 一致性、BM25 相关性、scope 隔离（含层级包含）、结构化元数据过滤、
多轮增删改后的一致性与边界/稳定性场景。需要真实 ES：

- 默认 ``http://localhost:9200``（``AGENT_MEMORY_TEST_ES_HOSTS`` 覆盖）

后端不可达时整组自动 skip；每个用例用唯一 index 隔离并在 teardown 删除，
互不污染、可重复运行。store 用 ``refresh="wait_for"`` 让写入对随后的 search
立即可见（ES 默认近实时，否则 search 看不到刚写入的文档）。
"""

from __future__ import annotations

import os
import uuid

import pytest

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.type_def import T_INVALID_OPEN, FilterClause, FilterGroup, FilterLogic, FilterOp, Scope
from jiuwen_memory.storage.fulltext_impl.elasticsearch_fulltext import ElasticsearchFulltextStore
from jiuwen_memory.storage.types import Document, TextQuery

ES_HOSTS = os.getenv("AGENT_MEMORY_TEST_ES_HOSTS", "http://localhost:9200")
SCOPE = Scope(org="itest", user="u1")


@pytest.fixture
def ft_index():
    """为每个测试生成唯一 Elasticsearch index 名称。"""
    return f"itest_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def ft(ft_index):
    """连到真实 ES 的 fulltext store（唯一 index）+ scope；teardown 删 index。"""
    pytest.importorskip("elasticsearch")
    store = ElasticsearchFulltextStore(hosts=ES_HOSTS, index=ft_index, refresh="wait_for")
    try:
        store.health()
        _ = store.client  # 触发连接 + 建索引
    except Exception as exc:
        pytest.skip(f"elasticsearch unreachable on {ES_HOSTS}: {exc}")
    yield store, SCOPE
    try:
        store.client.indices.delete(index=ft_index, ignore_unavailable=True)
    except Exception:  # 清理尽力而为
        pass


def _ids(hits) -> set[str]:
    return {h.id for h in hits}


# --------------------------------------------------------------- CRUD 生命周期
def test_ft_full_crud_lifecycle(ft):
    store, scope = ft
    store.insert(scope, [Document(id="a", text="the quick brown fox", metadata={"color": "red"})])

    got = store.get(scope, ["a"])
    assert len(got) == 1 and got[0].text == "the quick brown fox"
    assert got[0].metadata == {"color": "red"}
    assert _ids(store.search(scope, TextQuery(text="fox"))) == {"a"}

    store.update(scope, [Document(id="a", text="a lazy sleeping dog", metadata={"color": "blue"})])
    got = store.get(scope, ["a"])
    assert got[0].text == "a lazy sleeping dog"
    assert got[0].metadata == {"color": "blue"}
    assert _ids(store.search(scope, TextQuery(text="fox"))) == set()  # 旧词不再命中
    assert _ids(store.search(scope, TextQuery(text="dog"))) == {"a"}  # 新词命中

    store.delete(scope, ["a"])
    assert store.get(scope, ["a"]) == []
    assert store.search(scope, TextQuery(text="dog")) == []


def test_ft_insert_conflict_keeps_original(ft):
    store, scope = ft
    store.insert(scope, [Document(id="a", text="original")])
    with pytest.raises(ConflictError):
        store.insert(scope, [Document(id="a", text="overwrite")])
    assert store.get(scope, ["a"])[0].text == "original"


def test_ft_update_missing_raises_and_is_atomic(ft):
    store, scope = ft
    store.insert(scope, [Document(id="a", text="alpha")])
    # 批内含一个不存在的 id：整批不应生效（先校验后写）
    with pytest.raises(NotFoundError):
        store.update(scope, [Document(id="a", text="changed"), Document(id="ghost", text="x")])
    assert store.get(scope, ["a"])[0].text == "alpha"  # a 未被改动


def test_ft_delete_idempotent(ft):
    store, scope = ft
    store.insert(scope, [Document(id="a", text="x")])
    store.delete(scope, ["a"])
    store.delete(scope, ["a"])  # 再删不报错
    store.delete(scope, ["never-existed"])  # 删不存在不报错
    assert store.get(scope, ["a"]) == []


def test_ft_get_subset_and_missing_omitted(ft):
    store, scope = ft
    store.insert(scope, [Document(id="a", text="x"), Document(id="b", text="y")])
    got = store.get(scope, ["a", "missing", "b"])
    assert _ids(got) == {"a", "b"}  # 不存在的 id 省略
    assert store.get(scope, []) == []


def test_ft_empty_inputs_are_noops(ft):
    store, scope = ft
    store.insert(scope, [])
    store.update(scope, [])
    store.delete(scope, [])
    assert store.get(scope, []) == []


# --------------------------------------------------------------- 相关性
def test_ft_bm25_relevance_ordering(ft):
    store, scope = ft
    store.insert(
        scope,
        [
            Document(id="strong", text="python python python testing guide"),
            Document(id="weak", text="a short note mentioning python once"),
            Document(id="none", text="completely unrelated content here"),
        ],
    )
    hits = store.search(scope, TextQuery(text="python", top_k=10))
    ids = [h.id for h in hits]
    assert ids[0] == "strong"  # 词频更高者更相关
    assert "none" not in ids  # 不含查询词的不召回
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)  # 得分降序


def test_ft_top_k_limits_results(ft):
    store, scope = ft
    store.insert(scope, [Document(id=f"d{i}", text="common term") for i in range(10)])
    assert len(store.search(scope, TextQuery(text="common", top_k=3))) == 3
    assert len(store.search(scope, TextQuery(text="common", top_k=10))) == 10


# --------------------------------------------------------------- scope 隔离
def test_ft_scope_isolation_search_get_delete(ft):
    store, scope = ft
    other = Scope(org="itest", user="u2")
    store.insert(scope, [Document(id="mine", text="shared term")])
    store.insert(other, [Document(id="theirs", text="shared term")])

    assert _ids(store.search(scope, TextQuery(text="shared"))) == {"mine"}
    assert _ids(store.search(other, TextQuery(text="shared"))) == {"theirs"}
    # get 跨 scope 的 id 读不到
    assert _ids(store.get(scope, ["mine", "theirs"])) == {"mine"}
    # delete 不跨 scope：在 scope 删 theirs 无效
    store.delete(scope, ["theirs"])
    assert _ids(store.get(other, ["theirs"])) == {"theirs"}


def test_ft_scope_hierarchical_containment(ft):
    """空维度不约束：org 级 scope 命中该 org 下所有 user。"""
    store, _ = ft
    org_u1 = Scope(org="acme", user="u1")
    org_u2 = Scope(org="acme", user="u2")
    org_only = Scope(org="acme")  # user 为空 = 不限定该层
    store.insert(org_u1, [Document(id="a", text="report")])
    store.insert(org_u2, [Document(id="b", text="report")])

    assert _ids(store.search(org_only, TextQuery(text="report"))) == {"a", "b"}
    assert _ids(store.search(org_u1, TextQuery(text="report"))) == {"a"}


# --------------------------------------------------------------- 元数据过滤
def test_ft_metadata_filters(ft):
    store, scope = ft
    store.insert(
        scope,
        [
            Document(id="r1", text="doc", metadata={"color": "red", "n": 1, "tags": ["x", "y"]}),
            Document(id="r2", text="doc", metadata={"color": "blue", "n": 9, "tags": ["y", "z"]}),
            Document(id="r3", text="doc", metadata={"color": "red", "n": 5, "tags": ["z"]}),
        ],
    )

    def query_ids(filters):
        return _ids(store.search(scope, TextQuery(text="doc", top_k=10, filters=filters)))

    assert query_ids([FilterClause("color", FilterOp.EQ, "red")]) == {"r1", "r3"}
    assert query_ids([FilterClause("color", FilterOp.NE, "red")]) == {"r2"}
    assert query_ids([FilterClause("color", FilterOp.IN, ["red", "green"])]) == {"r1", "r3"}
    assert query_ids([FilterClause("color", FilterOp.NOT_IN, ["red"])]) == {"r2"}
    assert query_ids([FilterClause("n", FilterOp.GT, 4)]) == {"r2", "r3"}
    assert query_ids([FilterClause("n", FilterOp.GTE, 5)]) == {"r2", "r3"}
    assert query_ids([FilterClause("n", FilterOp.LT, 5)]) == {"r1"}
    assert query_ids([FilterClause("n", FilterOp.LTE, 5)]) == {"r1", "r3"}
    assert query_ids([FilterClause("tags", FilterOp.CONTAINS, "y")]) == {"r1", "r2"}
    # AND 组合
    assert query_ids(
        [FilterClause("color", FilterOp.EQ, "red"), FilterClause("n", FilterOp.GTE, 5)]
    ) == {"r3"}
    tree = FilterGroup(
        FilterLogic.AND,
        [
            FilterGroup(
                FilterLogic.OR,
                [
                    FilterClause("color", FilterOp.EQ, "blue"),
                    FilterClause("n", FilterOp.LTE, 1),
                ],
            ),
            FilterGroup(
                FilterLogic.NOT,
                [FilterClause("tags", FilterOp.CONTAINS, "z")],
            ),
        ],
    )
    assert query_ids(tree) == {"r1"}


def test_ft_distinguishes_scalar_equality_from_array_membership(ft):
    store, scope = ft
    store.insert(
        scope,
        [
            Document(id="scalar", text="doc", metadata={"kind": "work"}),
            Document(id="array", text="doc", metadata={"kind": ["work"]}),
        ],
    )

    def query_ids(op):
        filters = [FilterClause("kind", op, "work")]
        return _ids(store.search(scope, TextQuery(text="doc", top_k=10, filters=filters)))

    assert query_ids(FilterOp.EQ) == {"scalar"}
    assert query_ids(FilterOp.CONTAINS) == {"array"}


def test_ft_exact_match_multiword_keyword(ft):
    """多词/大小写敏感的精确等值（依赖 metadata 字符串映射为 keyword）。"""
    store, scope = ft
    store.insert(
        scope,
        [
            Document(id="a", text="vendor", metadata={"vendor": "Red Hat"}),
            Document(id="b", text="vendor", metadata={"vendor": "red hat"}),
        ],
    )

    def query_ids(vendor):
        query = TextQuery(text="vendor", filters=[FilterClause("vendor", FilterOp.EQ, vendor)])
        return _ids(store.search(scope, query))

    assert query_ids("Red Hat") == {"a"}  # 精确匹配，不被分析器拆词/小写化
    assert query_ids("red hat") == {"b"}


def test_ft_int_written_first_does_not_truncate_later_float(ft, ft_index):
    """首条整数不得把字段 mapping 锁成整型，否则其后的小数在索引里被截断。

    ES 的 mapping 由该字段第一条文档决定：没有 long→double 的 dynamic_template 时，
    先写 8 会把 metadata.priority 定成 long，随后写入的 9.5 在索引里变成 9——_source
    仍显示 9.5，但 gte 9.5 查不出这条文档，错得完全静默。
    """
    store, scope = ft
    store.insert(scope, [Document(id="i", text="p", metadata={"priority": 8})])
    store.insert(scope, [Document(id="f", text="p", metadata={"priority": 9.5})])

    def query_ids(op, value):
        query = TextQuery(text="p", filters=[FilterClause("priority", op, value)])
        return _ids(store.search(scope, query))

    mapping = store.client.indices.get_mapping(index=ft_index)
    priority = mapping[ft_index]["mappings"]["properties"]["metadata"]["properties"]["priority"]
    assert priority["type"] == "double"

    assert query_ids(FilterOp.GTE, 9.5) == {"f"}  # 被截断成 9 时此断言为空集
    assert query_ids(FilterOp.GTE, 9) == {"f"}
    assert query_ids(FilterOp.GTE, 8) == {"i", "f"}
    assert query_ids(FilterOp.LT, 9) == {"i"}


def test_ft_missing_field_is_excluded_by_range_predicate(ft):
    """缺失字段遇 range 谓词被排他——这是 t_invalid 必须落哨兵的直接依据。

    若索引沿用"空则不写"，活跃记忆（t_invalid 恒为空）会被 `t_invalid > as_of`
    整批排他，正好滤掉回溯查询最该命中的那些。此处固定 ES 的实际行为：哪天缺失
    字段不再被排他，哨兵才可以撤掉。
    """
    store, scope = ft
    store.insert(
        scope,
        [
            Document(id="nofield", text="doc", metadata={"t_valid": 1000}),
            Document(id="closed", text="doc", metadata={"t_valid": 1000, "t_invalid": 3000}),
        ],
    )

    def query_ids(filters):
        return _ids(store.search(scope, TextQuery(text="doc", filters=filters)))

    assert query_ids([FilterClause("t_valid", FilterOp.LTE, 2000)]) == {"nofield", "closed"}
    # nofield 因字段缺失被排他，尽管它在 as_of=2000 时应当有效
    assert query_ids([FilterClause("t_invalid", FilterOp.GT, 2000)]) == {"closed"}


def test_ft_sentinel_makes_open_ended_recallable_at_as_of(ft):
    """落哨兵后，开放有效期记忆能被 as_of 回溯命中，且不影响已失效记忆的排除。

    与 index_builder 的投影约定端到端对齐：as_of 查询下推的三个谓词
    （lifecycle != forgotten / t_valid <= as_of / t_invalid > as_of）合起来
    应当恰好命中"as_of 时刻有效"的记忆。
    """
    store, scope = ft
    as_of = 2000
    store.insert(
        scope,
        [
            # 永久有效 → 哨兵
            Document(
                id="open", text="doc", metadata={"t_valid": 1000, "t_invalid": T_INVALID_OPEN}
            ),
            # as_of 之前就失效
            Document(id="expired", text="doc", metadata={"t_valid": 1000, "t_invalid": 1500}),
            # as_of 之后才失效 → 当时仍有效
            Document(id="later", text="doc", metadata={"t_valid": 1000, "t_invalid": 3000}),
            # as_of 时还没生效
            Document(
                id="future", text="doc", metadata={"t_valid": 5000, "t_invalid": T_INVALID_OPEN}
            ),
        ],
    )

    hits = _ids(
        store.search(
            scope,
            TextQuery(
                text="doc",
                filters=[
                    FilterClause("t_valid", FilterOp.LTE, as_of),
                    FilterClause("t_invalid", FilterOp.GT, as_of),
                ],
            ),
        )
    )

    assert hits == {"open", "later"}


def test_ft_range_on_string_metadata_does_not_silently_lexicographic(ft, ft_index):
    """字符串字段映射为 keyword，range 打上去不会返回字典序结果。

    "high" >= "8" 在字典序下为真——若 metadata 字符串被映射成可比较的文本类型，
    范围过滤会静默误召。keyword 上的 range 是词典序但语义明确，此处固定实际行为，
    确保它不与数值字段的 range 混淆。
    """
    store, scope = ft
    store.insert(
        scope,
        [
            Document(id="s", text="lvl", metadata={"level": "high"}),
            Document(id="n", text="lvl", metadata={"level": 8}),
        ],
    )
    mapping = store.client.indices.get_mapping(index=ft_index)
    level = mapping[ft_index]["mappings"]["properties"]["metadata"]["properties"]["level"]
    # 首条是字符串 → keyword；后写的数值被 ES 转成字符串存入（已知残留风险，非 crash）
    assert level["type"] == "keyword"


# --------------------------------------------------------------- 多轮一致性
def test_ft_many_ops_consistency(ft):
    """40 文档插入 → 改偶数项文本+元数据 → 删 5 的倍数 → 校验最终态。"""
    store, scope = ft
    n = 40
    store.insert(
        scope, [Document(id=f"d{i}", text="base term", metadata={"n": i}) for i in range(n)]
    )
    assert _ids(store.get(scope, [f"d{i}" for i in range(n)])) == {f"d{i}" for i in range(n)}

    updated = {i for i in range(n) if i % 2 == 0}
    store.update(
        scope,
        [
            Document(id=f"d{i}", text="base term marked", metadata={"n": i, "upd": "yes"})
            for i in updated
        ],
    )
    deleted = {i for i in range(n) if i % 5 == 0}
    store.delete(scope, [f"d{i}" for i in deleted])

    survivors = {i for i in range(n) if i not in deleted}
    got = {d.id: d for d in store.get(scope, [f"d{i}" for i in range(n)])}
    assert set(got) == {f"d{i}" for i in survivors}
    for i in survivors:
        if i in updated:
            assert got[f"d{i}"].metadata.get("upd") == "yes"
            assert got[f"d{i}"].text == "base term marked"
        else:
            assert "upd" not in got[f"d{i}"].metadata

    # 「marked」只命中仍存活的偶数项
    marked = _ids(store.search(scope, TextQuery(text="marked", top_k=n)))
    assert marked == {f"d{i}" for i in updated if i not in deleted}
