"""树展开的真实内存检索链路与可观测数据面替身。"""

from dataclasses import dataclass, field, replace

from jiuwen_memory.common.type_def import (
    HierarchyRole,
    MemoryUnit,
    ParsedQuery,
    RetrievalPipeline,
    Segment,
)
from jiuwen_memory.retrieval.expander import ExpandRequest
from jiuwen_memory.retrieval.expander_impl.default_expander import DefaultExpander
from tests.unit.retrieval.hierarchy_query_fixtures import (
    QUERY_SCOPE,
    HierarchyHarness,
    hierarchy_query,
    make_harness,
    tree_unit,
)


@dataclass
class TreeHarness:
    base: HierarchyHarness
    root: MemoryUnit
    children: list[MemoryUnit]
    expander: DefaultExpander

    def save(self, units: list[MemoryUnit]) -> None:
        for unit in units:
            self.base.domain.update(unit.scope, [unit])


def link(parent: MemoryUnit, children: list[MemoryUnit]) -> None:
    """测试输入的显式双向边，不在辅助函数内隐藏断言。"""
    parent.hierarchy.child_ids = [child.id for child in children]
    parent.hierarchy.child_scopes = [child.scope for child in children]
    for child in children:
        child.hierarchy.parent_id = parent.id
        child.hierarchy.parent_scope = parent.scope


def make_tree(pipeline: RetrievalPipeline = RetrievalPipeline.RECALL_GET_RANK) -> TreeHarness:
    """只有父建检索索引，子只能经结构边点读，不能误用第二次关键词搜索。"""
    base = make_harness(pipeline)
    root = tree_unit("root")
    root.layers.l0 = "root"
    root.layers.l1 = "root overview"
    children = [
        tree_unit("first", HierarchyRole.SNAPSHOT), tree_unit("second", HierarchyRole.SNAPSHOT),
    ]
    for index, child in enumerate(children):
        child.scope = replace(QUERY_SCOPE, session=f"session-{index}")
        child.segments = [Segment(content=f"parameter detail {index}: 500ms -> 2000ms")]
        child.layers.l0 = f"leaf-{index}"
        child.layers.l1 = f"leaf overview {index}"
    link(root, children)
    base.add([root])
    for child in children:
        base.domain.add(child.scope, [child])
    expander = DefaultExpander(base.domain)
    base.retriever.bind_expander(expander)
    return TreeHarness(base, root, children, expander)


def make_space_tree(name: str) -> TreeHarness:
    """各空间保留独立根正文与同名 id，验证根分组不依赖全局唯一 id。"""
    tree = make_tree()
    for unit in [tree.root, *tree.children]:
        unit.scope = replace(unit.scope, space=name)
    tree.root.segments = [Segment(content=f"recall evidence {name}")]
    link(tree.root, tree.children)
    for unit in [tree.root, *tree.children]:
        tree.base.domain.add(unit.scope, [unit])
    tree.base.keyword_builder.build([tree.root])
    tree.base.vector_builder.build([tree.root])
    return tree


@dataclass
class Collector:
    selected: list[MemoryUnit] = field(default_factory=list)
    depths: list[int] = field(default_factory=list)

    def select(self, unit: MemoryUnit, depth: int) -> bool:
        self.selected.append(unit)
        self.depths.append(depth)
        return True


def expand_request(root: MemoryUnit, collector: Collector, **overrides) -> ExpandRequest:
    """构造已经移除父角色条件的内部展开输入。"""
    request = hierarchy_query(hierarchy_role=None)
    query = ParsedQuery(
        raw=request.text, hierarchy_kind=request.hierarchy_kind,
        span_start=request.span_start, span_end=request.span_end,
    )
    values = {"root": root, "query": query, "depth": 1, "select": collector.select}
    values.update(overrides)
    return ExpandRequest(**values)
