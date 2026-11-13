# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""选择键在 API 边界消费，未知名称在空间扇出前拒绝。"""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import Context, Scope, SpaceSpec, ValidationError, assemble_runtime
from jiuwen_memory.control.application.query import MemoryQueryService
from jiuwen_memory.retrieval.types import RetrievalResult
from tests.support.whitebox import private
from tests.unit.api.fixtures import ROOT_SECURITY, security_for
from tests.unit.api.hierarchy_api_fixtures import HOME
from tests.unit.api.hierarchy_search_fixtures import space_search_config

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("spaces", [None, ["first", "second"], []])
@pytest.mark.parametrize("name", ["missing", "", "  ", 1, [], {}])
def test_invalid_selection_rejected_before_any_space_resolution(spaces, name, monkeypatch):
    runtime = assemble_runtime(config=space_search_config())
    api = runtime.api
    forbidden = Mock(side_effect=AssertionError("must reject before fanout"))
    monkeypatch.setattr(api, "_search_candidates", forbidden)
    monkeypatch.setattr(MemoryQueryService, "recall", forbidden)
    extensions = {"retrieval_pipeline": name}
    if spaces is not None:
        extensions["spaces"] = spaces
    try:
        with pytest.raises(ValidationError, match="retrieval_pipeline"):
            api.search("evidence", Context(HOME, extensions=extensions),
                       security=security_for(api, HOME))
        forbidden.assert_not_called()
    finally:
        runtime.close()


@pytest.mark.parametrize("across", [False, True])
def test_selection_and_custom_extensions_survive_boundary_without_mutation(across, monkeypatch):
    runtime = assemble_runtime(config=space_search_config())
    api = runtime.api
    received = []

    async def capture(_self, scope, query):
        received.append((scope, query))
        return RetrievalResult()

    monkeypatch.setattr(MemoryQueryService, "recall", capture)
    owner = Scope(org=HOME.org, user=HOME.user)
    extensions = {"retrieval_pipeline": "default", "custom": {"enabled": True}}
    try:
        if across:
            extensions["spaces"] = ["first", "second"]
            for name in extensions["spaces"]:
                api.create_space(SpaceSpec(org=HOME.org, space=name, owner=owner),
                                 security=ROOT_SECURITY)
        context = Context(owner, extensions=extensions)
        before = deepcopy(context)
        api.search("evidence", context, security=security_for(api, owner))
        assert context == before
        assert len(received) == (2 if across else 1)
        for _, query in received:
            assert query.pipeline == "default"
            assert query.extensions == {"custom": {"enabled": True}}
    finally:
        runtime.close()


@pytest.mark.parametrize("engine_kind", ["in_memory", "cloud", "agent"])
def test_pipeline_name_alone_applies_memory_type_before_default_isolation(engine_kind):
    from jiuwen_memory.api import MemoryUnit
    from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel
    from jiuwen_memory.common.security import internal_context
    from jiuwen_memory.common.type_def import Segment
    from tests.support.scoped_authenticator import ScopedAuthenticator
    from tests.unit.control.fixtures import selectable_retrieval_config

    config = selectable_retrieval_config(engine_kind)
    config["engine"]["default"]["params"]["retrieval_pipelines"] = {
        "default": {"retriever": "default", "exclude_memory_types": ["experience", "skill"]},
        "lessons": {"retriever": "default", "memory_types": ["experience"]},
    }
    kernel = _build_kernel(config=config)
    api = kernel.api
    scope = Scope(org="o", user="u")
    security = internal_context(ScopedAuthenticator(scope))
    try:
        private(private(api, "_engine"), "_index").build([MemoryUnit(
            id=kind, scope=scope, segments=[Segment(content="evidence")],
            system_metadata={"memory_type": kind},
        ) for kind in ["chat", "experience", "skill"]])
        for selected, expected in [("default", "chat"), ("lessons", "experience")]:
            context = Context(scope, extensions={"retrieval_pipeline": selected})
            before = deepcopy(context)
            result = api.search("evidence", context, security=security)
            assert {item.unit_id for item in result.items} == {expected}, "只选流程名即可隔离类型"
            assert context == before, "API不修改调用方上下文"
        conflict = api.search(
            "evidence", Context(scope, extensions={"retrieval_pipeline": "lessons"}),
            security=security, filters={"memory_type": "chat"},
        )
        assert conflict.items == [], "调用方过滤只能收窄，不能覆盖固定类型边界"
    finally:
        kernel.close()


@pytest.mark.parametrize("route_override", [None, "chat"])
def test_experience_name_preserves_single_space_constraint(route_override, monkeypatch):
    config = space_search_config()
    config["engine"]["default"]["params"]["retrieval_pipelines"] = {
        "lessons": {"retriever": "default", "memory_types": ["experience"]},
    }
    runtime = assemble_runtime(config=config)
    api = runtime.api
    forbidden = Mock(side_effect=AssertionError("must not recall across experience spaces"))
    monkeypatch.setattr(MemoryQueryService, "recall", forbidden)
    extensions = {"retrieval_pipeline": "lessons", "spaces": []}
    if route_override is not None:
        extensions["memory_type"] = route_override
    try:
        with pytest.raises(ValidationError, match="一次只查一个空间"):
            api.search(
                "evidence", Context(HOME, extensions=extensions),
                security=security_for(api, HOME),
            )
        forbidden.assert_not_called()
    finally:
        runtime.close()
