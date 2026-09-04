"""文档记忆召回（``ShadowRecaller``）——通道语义、构造校验与 RRF 合并。

文档模式唯一真正命中的召回路：channel()=DOCUMENT，fulltext 必跑、vector 按
``shadow.vec_enabled`` 补充，两路按名次 RRF 合并。失效方向：

- ``channel()=DOCUMENT`` 但 parser 默认 channels 不含 DOCUMENT，retriever 未补全时
  本算子被 ``r.channel() in channels`` 过滤 → 召回落空（F08 §5.6 S14）。
- RRF 合并若按原始分数 max 归并，两路量纲相反（bm25 负值越小越相关 / 距离越小越相关）
  会让排序整体颠倒；必须只消费名次。
- 构造期无 shadow 端口应 fail-closed，而非拖到首次 recall 以 AttributeError 暴露。
"""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为

import pytest

from jiuwen_memory.common.errors import UnsupportedStorageCapabilityError
from jiuwen_memory.common.type_def import ParsedQuery, RecallChannel, Scope
from jiuwen_memory.retrieval.recaller_impl.shadow_recaller import ShadowRecaller
from jiuwen_memory.storage.types import ScoredID, TextQuery, VectorQuery

pytestmark = pytest.mark.unit

SCOPE = Scope(org="acme", user="u1")


class _FakeShadow:
    """可脚本化的影子索引：记录查询、按脚本返 fulltext/vector 结果。"""

    def __init__(
        self,
        *,
        vec_enabled: bool = False,
        fulltext: list[ScoredID] | None = None,
        vector: list[ScoredID] | None = None,
    ) -> None:
        self.vec_enabled = vec_enabled
        self._fulltext = fulltext or []
        self._vector = vector or []
        self.fulltext_queries: list[TextQuery] = []
        self.vector_queries: list[VectorQuery] = []

    def search_fulltext(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        self.fulltext_queries.append(query)
        return self._fulltext

    def search_vector(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        self.vector_queries.append(query)
        return self._vector


class _FakeStorage:
    def __init__(self, shadow: _FakeShadow | None) -> None:
        self._shadow = shadow

    def has_shadow_port(self, name: str = "default") -> bool:
        return self._shadow is not None

    def shadow_index_port(self, name: str = "default") -> _FakeShadow:
        return self._shadow  # type: ignore[return-value]


def _sid(uid: str, score: float = 0.0) -> ScoredID:
    return ScoredID(id=uid, score=score)


# -- 通道与构造 -------------------------------------------------------------- #


def test_the_channel_is_document() -> None:
    """文档召回是独立第四路；retriever 据此决定是否补 DOCUMENT 进 enabled channels。"""
    shadow = _FakeShadow()
    recaller = ShadowRecaller(_FakeStorage(shadow))
    assert recaller.channel() is RecallChannel.DOCUMENT


def test_constructor_requires_a_shadow_port() -> None:
    """无 shadow 端口装配即抛，不拖到首次 recall（fail-closed）。"""
    with pytest.raises(UnsupportedStorageCapabilityError, match="shadow"):
        ShadowRecaller(_FakeStorage(None))


# -- RRF 合并 ---------------------------------------------------------------- #


def test_merge_rrf_orders_by_rank_not_raw_score() -> None:
    """名次即列表下标；原始分数（bm25 负值 / 距离负值）不参与比较。"""
    merged = ShadowRecaller._merge_rrf(
        [[_sid("a", score=-100.0), _sid("b", score=-5.0)]], top_k=10
    )
    assert [s.unit_id for s in merged] == ["a", "b"]
    assert all(s.channel is RecallChannel.DOCUMENT for s in merged)


def test_merge_rrf_accumulates_contribution_across_paths() -> None:
    """同一 unit 两路都命中时贡献累加——多路一致更可信，排到最前。"""
    merged = ShadowRecaller._merge_rrf(
        [
            [_sid("a"), _sid("b")],
            [_sid("a"), _sid("c")],
        ],
        top_k=10,
    )
    assert merged[0].unit_id == "a"  # a 两路命中，贡献高于单路 b/c
    assert {s.unit_id for s in merged} == {"a", "b", "c"}


def test_merge_rrf_truncates_to_top_k() -> None:
    merged = ShadowRecaller._merge_rrf([[_sid("a"), _sid("b"), _sid("c")]], top_k=2)
    assert [s.unit_id for s in merged] == ["a", "b"]


def test_merge_rrf_of_empty_paths_is_empty() -> None:
    assert ShadowRecaller._merge_rrf([[], []], top_k=10) == []


# -- recall 主流程 ----------------------------------------------------------- #


def test_recall_runs_fulltext_and_skips_vector_when_disabled() -> None:
    """降级模式（vec_enabled=False）只走 fulltext；query.vector 有值也不跑 ANN。"""
    shadow = _FakeShadow(fulltext=[_sid("u1", -3.0), _sid("u2", -8.0)])
    recaller = ShadowRecaller(_FakeStorage(shadow))
    query = ParsedQuery(raw="deploy cluster", rewritten="", vector=[0.1, 0.2])

    result = recaller.recall(SCOPE, query, top_k=10)

    assert [s.unit_id for s in result] == ["u1", "u2"]
    assert len(shadow.fulltext_queries) == 1
    assert shadow.fulltext_queries[0].text == "deploy cluster"
    assert shadow.vector_queries == []  # 降级不跑向量路


def test_recall_uses_rewritten_query_when_present() -> None:
    """fulltext 用 rewritten 优先于 raw（与 KeywordRecaller 同口径）。"""
    shadow = _FakeShadow(fulltext=[_sid("u1")])
    recaller = ShadowRecaller(_FakeStorage(shadow))
    query = ParsedQuery(raw="raw words", rewritten="rewritten words")

    recaller.recall(SCOPE, query, top_k=10)

    assert shadow.fulltext_queries[0].text == "rewritten words"


def test_recall_adds_vector_path_when_enabled_and_vector_present() -> None:
    """完整模式（vec_enabled=True）+ query.vector 非空时补跑 ANN，两路 RRF 合并。"""
    shadow = _FakeShadow(
        vec_enabled=True,
        fulltext=[_sid("a"), _sid("b")],
        vector=[_sid("a"), _sid("c")],
    )
    recaller = ShadowRecaller(_FakeStorage(shadow))
    query = ParsedQuery(raw="deploy", vector=[0.5])

    result = recaller.recall(SCOPE, query, top_k=10)

    assert len(shadow.vector_queries) == 1
    assert result[0].unit_id == "a"  # 两路命中，贡献最高
