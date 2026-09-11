"""上卷测试仅注入公开依赖，不读取或修改生产对象的保护成员。"""

from dataclasses import dataclass, replace
from unittest.mock import Mock

from jiuwen_memory.common.type_def import (
    HierarchyRole,
    MemoryUnit,
    ParsedQuery,
    RetrievalPipeline,
    ScoredMemoryUnit,
    Segment,
)
from jiuwen_memory.retrieval.expander_impl.default_expander import DefaultExpander
from jiuwen_memory.retrieval.retriever_impl.hierarchy_rollup import RollupRequest
from tests.unit.retrieval.expansion_fixtures import TreeHarness, link
from tests.unit.retrieval.hierarchy_query_fixtures import (
    QUERY_SCOPE,
    hierarchy_query,
    make_harness,
    tree_unit,
)


@dataclass
class RollupStore:
    units: list[MemoryUnit]

    def get(self, scope, unit_ids):
        return [unit for unit in self.units if unit.scope == scope and unit.id in unit_ids]


def rollup_input(candidates: list[ScoredMemoryUnit], **overrides) -> RollupRequest:
    """默认输入是允许后代的候选查询，输出为 time_span。"""
    query = hierarchy_query()
    values = {
        "scope": QUERY_SCOPE,
        "query": ParsedQuery(raw=query.text, hierarchy_kind=query.hierarchy_kind),
        "target_role": query.hierarchy_role,
        "candidates": candidates,
    }
    values.update(overrides)
    return RollupRequest(**values)


def read_spy(units: list[MemoryUnit]) -> Mock:
    """公开 get 的只读替身，结果保留完整 Scope，便于核对读取边界。"""
    return Mock(wraps=RollupStore(units))


def dropped_filters_parser(parser, query):
    """模拟旧 parser 丢弃过滤字段，上卷仍必须保留调用方限制。"""
    return parser.parse(replace(query, filters=None, as_of=None))


def make_rollup_space(name: str) -> TreeHarness:
    """每个空间只有子建索引，父须经上卷准入，正文用于跨空间去重区分。"""
    base = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    root = tree_unit("parent")
    root.segments = [Segment(content=f"rollup parent {name}")]
    root.layers.l0 = "root"
    children = [tree_unit(f"leaf-{index}", HierarchyRole.SNAPSHOT) for index in range(2)]
    for unit in [root, *children]:
        unit.scope = replace(QUERY_SCOPE, space=name)
        unit.user_metadata["allowed"] = name
    for child in children:
        child.layers.l0 = "leaf"
    link(root, children)
    base.domain.add(root.scope, [root, *children])
    base.keyword_builder.build(children)
    base.vector_builder.build(children)
    expander = DefaultExpander(base.domain)
    base.retriever.bind_expander(expander)
    return TreeHarness(base, root, children, expander)
