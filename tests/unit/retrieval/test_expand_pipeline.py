"""三条检索路径均在 top-k 后展开，且使用真实披露字段控制共享预算。"""

from dataclasses import replace

import pytest

from jiuwen_memory.common.errors import UnsupportedCapabilityError, ValidationError
from jiuwen_memory.common.type_def import (
    FilterClause,
    FilterOp,
    HierarchyRole,
    RecallChannel,
    RetrievalPipeline,
    Segment,
)
from jiuwen_memory.retrieval.expansion import (
    PreparedRetrievalResult,
    complete_expansion,
    disclosed_tokens,
)
from jiuwen_memory.retrieval.retriever_impl.multimodal_retriever import MultimodalRetriever
from jiuwen_memory.retrieval.types import DisclosureLevel
from tests.unit.retrieval.expansion_fixtures import link, make_tree
from tests.unit.retrieval.hierarchy_query_fixtures import QUERY_SCOPE, hierarchy_query, make_harness

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("pipeline", list(RetrievalPipeline))
@pytest.mark.parametrize("channel", [RecallChannel.KEYWORD, RecallChannel.VECTOR])
def test_parent_top_k_does_not_consume_expanded_evidence_slots(pipeline, channel) -> None:
    tree = make_tree(pipeline)
    query = hierarchy_query(top_k=1, expand_depth=1, channels=[channel])
    plain = tree.base.retriever.retrieve(QUERY_SCOPE, replace(query, expand_depth=0))
    expanded = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    assert [item.unit_id for item in plain.items] == ["root"]
    assert [item.unit_id for item in expanded.items] == ["root", "first", "second"]
    assert expanded.items[1].parent_id == "root"
    assert expanded.items[1].score == expanded.items[0].score
    assert expanded.errors == []


@pytest.mark.parametrize("level", list(DisclosureLevel))
def test_budget_includes_roots_and_uses_actual_disclosed_fields(level) -> None:
    tree = make_tree()
    query = hierarchy_query(expand_depth=1, disclosure=level, max_tokens=4)
    result = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    assert sum(disclosed_tokens(item) for item in result.items) <= 4
    assert any(error.error_type == "budget_exhausted" for error in result.errors)


def test_root_can_exhaust_budget_before_any_child_is_read(monkeypatch) -> None:
    tree = make_tree()
    calls = []
    original = tree.base.domain.get

    def observed_get(request_scope, ids):
        calls.append(request_scope)
        return original(request_scope, ids)

    monkeypatch.setattr(tree.base.domain, "get", observed_get)
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        expand_depth=1, max_tokens=1, with_trajectory=True,
    ))
    assert [item.unit_id for item in result.items] == ["root"]
    assert all(scope.session == "" for scope in calls)
    assert result.trajectory[-1].detail["visited_count"] == "0"


def test_bad_branch_is_observable_without_trajectory() -> None:
    tree = make_tree()
    tree.root.hierarchy.child_ids[0] = "absent"
    tree.save([tree.root])
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(expand_depth=1))
    assert [item.unit_id for item in result.items] == ["root", "second"]
    assert result.trajectory == []
    assert result.errors[0].channel is RecallChannel.HIERARCHY
    assert result.errors[0].error_type == "missing_child"


def test_deferred_retrieval_only_returns_roots_until_completion() -> None:
    tree = make_tree()
    query = hierarchy_query(expand_depth=1, defer_expansion=True)
    prepared = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    assert isinstance(prepared, PreparedRetrievalResult)
    assert [item.unit_id for item in prepared.items] == ["root"]
    result = complete_expansion(prepared, [prepared], query)
    assert [item.unit_id for item in result.items] == ["root", "first", "second"]
    assert not isinstance(result, PreparedRetrievalResult)


def test_expansion_without_configured_operator_is_not_silently_ignored() -> None:
    base = make_harness(RetrievalPipeline.RECALL_GET_RANK)
    with pytest.raises(UnsupportedCapabilityError, match="Expander"):
        base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(expand_depth=1))


def test_leaf_root_does_not_imply_extra_results_or_truncation() -> None:
    tree = make_tree()
    tree.root.hierarchy.role = HierarchyRole.SNAPSHOT
    tree.root.hierarchy.child_ids = []
    tree.root.hierarchy.child_scopes = []
    tree.save([tree.root])
    tree.base.keyword_builder.update([tree.root])
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        hierarchy_role=HierarchyRole.SNAPSHOT, expand_depth=8, with_trajectory=True,
    ))
    assert [item.unit_id for item in result.items] == ["root"]
    assert result.errors == []
    assert result.trajectory[-1].detail["actual_depth"] == "0"


def test_parser_cannot_disable_expansion_or_drop_child_permission_filter(monkeypatch) -> None:
    tree = make_tree()
    tree.root.user_metadata["visible"] = True
    tree.children[1].user_metadata["visible"] = True
    tree.save([tree.root, *tree.children])
    original_parse = tree.base.parser.parse

    def dropping_parse(request):
        request.expand_depth = 0
        parsed = original_parse(request)
        parsed.scalar_filters = None
        return parsed

    monkeypatch.setattr(tree.base.parser, "parse", dropping_parse)
    query = hierarchy_query(
        expand_depth=1, filters=FilterClause("user_metadata.visible", FilterOp.EQ, True),
    )
    result = tree.base.retriever.retrieve(QUERY_SCOPE, query)
    assert [item.unit_id for item in result.items] == ["root", "second"]
    assert query.expand_depth == 1


def test_l0_fallback_budget_does_not_charge_full_content() -> None:
    tree = make_tree()
    for child in tree.children:
        child.layers.l0 = ""
        child.segments = [Segment(content="detail " * 1000)]
    tree.save(tree.children)
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
        expand_depth=1, max_tokens=22, disclosure=DisclosureLevel.L0,
    ))
    assert [item.unit_id for item in result.items] == ["root", "first"]
    assert sum(disclosed_tokens(item) for item in result.items) == 22
    assert len(result.items[1].content) == 7000


def test_same_id_children_are_rendered_from_their_own_scope() -> None:
    tree = make_tree()
    tree.children[1].id = tree.children[0].id
    link(tree.root, tree.children)
    tree.save([tree.root])
    tree.base.domain.add(tree.children[1].scope, [tree.children[1]])
    result = tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(expand_depth=1))
    assert [item.unit_id for item in result.items] == ["root", "first", "first"]
    assert [item.abstract for item in result.items[1:]] == ["leaf-0", "leaf-1"]


def test_expansion_requires_a_valid_disclosure_level() -> None:
    tree = make_tree()
    with pytest.raises(ValidationError, match="disclosure"):
        tree.base.retriever.retrieve(QUERY_SCOPE, hierarchy_query(
            expand_depth=1, disclosure="bad",
        ))


def test_multimodal_wrapper_explicitly_rejects_unadapted_expansion() -> None:
    tree = make_tree()
    with pytest.raises(UnsupportedCapabilityError, match="MultimodalRetriever"):
        MultimodalRetriever(tree.base.retriever).retrieve(
            QUERY_SCOPE, hierarchy_query(expand_depth=1),
        )
