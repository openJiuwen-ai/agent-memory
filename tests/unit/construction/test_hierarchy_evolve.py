# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""EvolveRequest 的结构分派、真实替换和具名 Factory 兼容性。"""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import HierarchyRole, LifecycleState, validate_tree
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.construction.evolver import EvolveMode, EvolveRequest, EvolverProducer
from jiuwen_memory.construction.evolver_impl.dynamic_evolver import DynamicEvolver
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import OrchestratingEvolver
from jiuwen_memory.construction.evolver_impl.schema_orchestrating_evolver import (
    SchemaOrchestratingEvolver,
)
from jiuwen_memory.construction.hierarchy_composer import (
    HierarchyComposer,
    HierarchyComposeResult,
    HierarchyComposerProducer,
    HierarchyRepair,
)
from jiuwen_memory.construction.index_builder import IndexBuilderProducer
from jiuwen_memory.storage.store_manager import StoreManagerProducer
from jiuwen_memory.storage.types import IndexRemoveMode, IndexWriteMode
from tests.unit.construction.hierarchy_evolve_fixtures import (
    make_evolve_request,
    make_evolver_harness,
    make_factory_context,
)
from tests.unit.construction.hierarchy_fixtures import make_harness, make_leaf

pytestmark = pytest.mark.unit

_TARGETS = [
    ("orchestrating", OrchestratingEvolver),
    ("dynamic", DynamicEvolver),
    ("schema_orchestrating", SchemaOrchestratingEvolver),
]


@pytest.fixture(autouse=True)
def isolated_factory_instances():
    Factory.reset_all()
    try:
        yield
    finally:
        Factory.reset_all()


def test_real_composer_builds_and_replaces_through_evolver_without_content_operators() -> None:
    leaves = [make_leaf("first"), make_leaf("second", 20)]
    composition = make_harness(leaves)
    harness = make_evolver_harness(composition, composition.composer)
    request = make_evolve_request(leaves)
    original = deepcopy(request)

    first = harness.evolver.evolve(request)

    assert first.hierarchy_result.complete
    assert len(first.hierarchy_result.created_parent_ids) == 1
    assert request == original
    options = request.hierarchy_options
    first_parent_id = first.hierarchy_result.created_parent_ids[0]
    old_parent = composition.read(options.tree_home_scope, first_parent_id)
    current_leaves = [composition.read(leaf.scope, leaf.id) for leaf in leaves]
    replacing = make_evolve_request(current_leaves, [old_parent])
    replacement_before = deepcopy(replacing)

    second = harness.evolver.evolve(replacing)

    assert second.hierarchy_result.complete
    assert second.hierarchy_result.replaced_parent_ids == [first_parent_id]
    assert second.hierarchy_result.created_parent_ids != [first_parent_id]
    assert replacing == replacement_before
    new_parent = composition.read(
        options.tree_home_scope, second.hierarchy_result.created_parent_ids[0],
    )
    stored_leaves = [composition.read(leaf.scope, leaf.id) for leaf in leaves]
    validate_tree([new_parent, *stored_leaves])
    assert new_parent.hierarchy.child_ids == ["first", "second"]
    assert composition.read(old_parent.scope, old_parent.id).lifecycle is LifecycleState.ARCHIVED
    assert new_parent.system_metadata["build_source"] == "manual"
    for persisted in [new_parent, *stored_leaves]:
        assert "request_trace" not in persisted.system_metadata
        assert "request_trace" not in persisted.user_metadata
    for persisted, untouched in zip(stored_leaves, original.units):
        persisted.hierarchy = deepcopy(untouched.hierarchy)
        assert persisted == untouched
    assert harness.dependencies.extractor.mock_calls == []
    assert harness.dependencies.dedup.mock_calls == []
    assert harness.dependencies.abstractor.mock_calls == []
    assert harness.dependencies.associator.mock_calls == []
    assert harness.dependencies.llm.mock_calls == []


def test_dispatch_separates_leaves_and_old_parents_and_preserves_incomplete_report() -> None:
    leaves = [make_leaf("first"), make_leaf("second", 20)]
    old_parent = make_leaf("old")
    old_parent.hierarchy.role = HierarchyRole.TIME_SPAN
    expected = HierarchyComposeResult(
        created_parent_ids=["new"], updated_child_ids=["first"],
        replaced_parent_ids=["old"], complete=False,
        repair_required=[HierarchyRepair("second", "index_update_failed", "new", "old")],
    )
    composer = Mock(spec=HierarchyComposer)
    composer.replace_in_span.return_value = expected
    composition = make_harness(leaves)
    harness = make_evolver_harness(composition, composer)
    request = make_evolve_request(leaves, [old_parent])
    request.units = [leaves[0], old_parent, leaves[1]]
    before = deepcopy(request)

    result = harness.evolver.evolve(request)

    assert result.hierarchy_result is expected
    assert result.hierarchy_result.complete is False
    assert result.hierarchy_result.repair_required == expected.repair_required
    assert result.created_ids == result.updated_ids == result.superseded_ids == []
    assert result.forgotten_ids == result.created_units == []
    composer.build.assert_not_called()
    assert composer.replace_in_span.call_count == 1
    submitted = composer.replace_in_span.call_args.args[0]
    assert submitted.leaves == leaves
    assert submitted.existing_parents == [old_parent]
    assert submitted.options is request.hierarchy_options
    assert submitted.options.metadata == {"build_source": "manual"}
    assert request == before
    assert harness.dependencies.extractor.mock_calls == []
    assert harness.dependencies.dedup.mock_calls == []
    assert composition.builder.calls == []


@pytest.mark.parametrize(
    ("invalid_case", "message"),
    [
        ("missing_options", "hierarchy_options"),
        ("invalid_options_type", "hierarchy_options"),
        ("missing_composer", "HierarchyComposer"),
        ("wrong_role", "角色"),
        ("plain_request", "EvolveRequest"),
        ("invalid_mode", "EvolveMode"),
        ("invalid_units", "units.*列表"),
        ("invalid_unit", "MemoryUnit"),
        ("invalid_hierarchy", "HierarchyRef"),
        ("invalid_parent_roles", "parent_roles.*列表"),
    ],
)
def test_invalid_hierarchy_dispatch_is_zero_write(invalid_case, message) -> None:
    leaf = make_leaf("leaf")
    composition = make_harness([leaf])
    composer = Mock(spec=HierarchyComposer)
    selected = None if invalid_case == "missing_composer" else composer
    harness = make_evolver_harness(composition, selected)
    request = make_evolve_request([leaf])
    if invalid_case == "missing_options":
        request.hierarchy_options = None
    elif invalid_case == "invalid_options_type":
        request.hierarchy_options = {}
    elif invalid_case == "wrong_role":
        request.units[0].hierarchy.role = HierarchyRole.SCENE
    elif invalid_case == "plain_request":
        request = [leaf]
    elif invalid_case == "invalid_mode":
        request.mode = "hierarchy"
    elif invalid_case == "invalid_units":
        request.units = tuple(request.units)
    elif invalid_case == "invalid_unit":
        request.units = [{}]
    elif invalid_case == "invalid_hierarchy":
        request.units[0].hierarchy = None
    elif invalid_case == "invalid_parent_roles":
        request.hierarchy_options.parent_roles = tuple(request.hierarchy_options.parent_roles)
    before = deepcopy(request)

    with pytest.raises(ValidationError, match=message):
        harness.evolver.evolve(request)

    assert request == before
    assert composer.mock_calls == []
    assert composition.builder.calls == []
    assert harness.dependencies.extractor.mock_calls == []
    assert harness.dependencies.dedup.mock_calls == []


@pytest.mark.parametrize(
    "mode", [EvolveMode.EXTRACT, EvolveMode.CONSOLIDATE, EvolveMode.ASSOCIATE, EvolveMode.FORGET],
)
def test_non_hierarchy_modes_reject_hierarchy_options_before_any_operator(mode) -> None:
    leaf = make_leaf("leaf")
    composition = make_harness([leaf])
    composer = Mock(spec=HierarchyComposer)
    harness = make_evolver_harness(composition, composer)
    request = make_evolve_request([leaf])
    request.mode = mode

    with pytest.raises(ValidationError, match="仅 HIERARCHY"):
        harness.evolver.evolve(request)

    assert composer.mock_calls == []
    assert composition.builder.calls == []
    assert harness.dependencies.extractor.mock_calls == []
    assert harness.dependencies.abstractor.mock_calls == []
    assert harness.dependencies.associator.mock_calls == []
    assert harness.dependencies.dedup.mock_calls == []


@pytest.mark.parametrize(("target", "evolver_type"), _TARGETS)
def test_factory_selected_profile_and_shared_index_builder_are_effective(target, evolver_type):
    context = make_factory_context()
    leaves = [make_leaf("first"), make_leaf("second", 20)]
    composition = make_harness(leaves)
    StoreManagerProducer.put("default", composition.manager)
    IndexBuilderProducer.put("shared", composition.builder)
    configured = AssemblyContext.from_dict({
        "hierarchy_composer": {
            "selected": {
                "target": "default",
                "params": {
                    "index_builder": "shared",
                    "hierarchy_profiles": {"time": {
                        "parent_roles": ["time_span"],
                        "stage_options": {"TimeSpanMerger": {"gap_seconds": "60"}},
                    }},
                },
            },
        },
    })
    context = context.merged(configured)
    evolver = EvolverProducer.build(target, {
        "extractor": {"target": "keyword"},
        "dedup": {"target": "keyword"},
        "index_builder": "shared", "hierarchy_composer": "selected",
    }, context)
    composer = HierarchyComposerProducer.build_named("selected", context)

    result = evolver.evolve(make_evolve_request(leaves))

    assert type(evolver) is evolver_type
    assert composer.index_builder is composition.builder
    assert IndexBuilderProducer.build_named("shared", context) is composition.builder
    assert result.hierarchy_result.complete
    assert len(result.hierarchy_result.created_parent_ids) == 2, "60 秒间隔配置应拆成两个父"
    home = make_evolve_request(leaves).hierarchy_options.tree_home_scope
    parents = [composition.read(home, uid) for uid in result.hierarchy_result.created_parent_ids]
    assert [parent.hierarchy.child_ids for parent in parents] == [["first"], ["second"]]
    assert any(call.method == "build" for call in composition.builder.calls)

    # 使用另一路普通内容演进，证明 Evolver 与 Composer 的写入经过同一具名 builder。
    composition.builder.calls.clear()
    retired = deepcopy(parents[0])
    retired.lifecycle = LifecycleState.SUPERSEDED
    forgotten = evolver.evolve(EvolveRequest(units=[retired], mode=EvolveMode.FORGET))

    assert forgotten.forgotten_ids == [retired.id]
    assert [(call.method, call.mode) for call in composition.builder.calls] == [
        ("update", IndexWriteMode.FORWARD_ONLY), ("remove", IndexRemoveMode.SOFT),
    ]
    assert composition.read(retired.scope, retired.id).lifecycle is LifecycleState.FORGOTTEN


@pytest.mark.parametrize(("target", "evolver_type"), _TARGETS)
def test_factory_does_not_enable_hierarchy_composer_by_default(target, evolver_type) -> None:
    context = make_factory_context()
    evolver = EvolverProducer.build(target, {"extractor": {"target": "keyword"}}, context)

    assert type(evolver) is evolver_type
    assert "hierarchy_composer" not in context.namespaces
    with pytest.raises(ValidationError, match="未装配 HierarchyComposer"):
        evolver.evolve(make_evolve_request([make_leaf("leaf")]))


def test_factory_composer_requires_explicit_index_builder() -> None:
    context = make_factory_context()

    with pytest.raises(ValidationError, match="index_builder.*未配置"):
        HierarchyComposerProducer.build("default", {}, context)


@pytest.mark.parametrize("target", [item[0] for item in _TARGETS])
@pytest.mark.parametrize("invalid_selection", [False, 0, {}, []])
def test_factory_rejects_invalid_composer_selection_instead_of_disabling(
    target, invalid_selection,
) -> None:
    """错误类型或空内联配置不能被当作关闭开关静默吞掉。"""
    context = make_factory_context()

    with pytest.raises(ValidationError, match="HierarchyComposerProducer.dep"):
        EvolverProducer.build(target, {
            "extractor": {"target": "keyword"},
            "hierarchy_composer": invalid_selection,
        }, context)
