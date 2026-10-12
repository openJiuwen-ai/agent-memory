# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Evolver 层级分派测试的公开依赖装配与请求辅助。"""

from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import Mock

from jiuwen_memory.common.bootstrap import register_plugins
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.type_def import MemoryUnit
from jiuwen_memory.config.context import AssemblyContext
from jiuwen_memory.config.defaults import default_context
from jiuwen_memory.construction.abstractor import Abstractor
from jiuwen_memory.construction.associator import Associator
from jiuwen_memory.construction.bootstrap import register_constructors
from jiuwen_memory.construction.dedup import Dedup
from jiuwen_memory.construction.evolver import EvolveMode, EvolveRequest
from jiuwen_memory.construction.evolver_impl.orchestrating_evolver import (
    EvolverDependencies,
    OrchestratingEvolver,
)
from jiuwen_memory.construction.extractor import Extractor
from jiuwen_memory.construction.hierarchy_composer import HierarchyComposer
from jiuwen_memory.construction.schema_bootstrap import register_schema_constructors
from jiuwen_memory.retrieval.bootstrap import register_operators
from jiuwen_memory.storage.bootstrap import register_backends
from tests.unit.construction.hierarchy_fixtures import (
    ORIGIN,
    CompositionHarness,
    make_request,
)


@dataclass
class HierarchyEvolveHarness:
    evolver: OrchestratingEvolver
    dependencies: EvolverDependencies


def make_evolver_harness(
    composition: CompositionHarness, composer: HierarchyComposer | None,
) -> HierarchyEvolveHarness:
    """显式注入 Composer，其余内容算子只记录调用以检测错误分派。"""
    dependencies = EvolverDependencies(
        extractor=Mock(spec=Extractor),
        abstractor=Mock(spec=Abstractor),
        associator=Mock(spec=Associator),
        index_builder=composition.builder,
        storage=composition.manager,
        message_store=composition.manager.kv(),
        dedup=Mock(spec=Dedup),
        llm=Mock(spec=LLM),
        hierarchy_composer=composer,
    )
    return HierarchyEvolveHarness(OrchestratingEvolver(dependencies), dependencies)


def make_evolve_request(
    leaves: list[MemoryUnit], parents: list[MemoryUnit] | None = None,
) -> EvolveRequest:
    """首次与替换共用有界区间；请求上下文与父元数据使用不同键。"""
    composition_request = make_request(
        leaves, parents=parents, span=(ORIGIN, ORIGIN + timedelta(hours=1)),
    )
    return EvolveRequest(
        units=[*leaves, *(parents or [])],
        mode=EvolveMode.HIERARCHY,
        metadata={"request_trace": "evolve-only"},
        hierarchy_options=composition_request.options,
    )


def make_factory_context() -> AssemblyContext:
    """使用公开分层 bootstrap，保留真实 Factory 的注册和具名解析。"""
    register_plugins()
    register_backends()
    register_operators()
    register_constructors()
    register_schema_constructors()
    return default_context()
