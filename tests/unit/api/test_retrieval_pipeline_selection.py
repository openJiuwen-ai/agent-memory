# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""选择键在 API 边界消费，未知名称在空间扇出前拒绝。"""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from jiuwen_memory.api import Context, Scope, SpaceSpec, ValidationError, assemble_runtime
from jiuwen_memory.control.application.query import MemoryQueryService
from jiuwen_memory.retrieval.types import RetrievalResult
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
