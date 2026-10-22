# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""默认子排序惰性共享原装配依赖，不复用更新后的 Factory 缓存。"""

from unittest.mock import Mock

import pytest

from jiuwen_memory.api import Context, assemble_runtime
from jiuwen_memory.common.embedder.base import EmbedderProducer
from jiuwen_memory.common.factory.factory import Factory
from tests.unit.api.fixtures import HOME, security_for
from tests.unit.api.hierarchy_api_fixtures import runtime_config, task_options, write_snapshot

pytestmark = pytest.mark.unit


def test_default_tree_ranking_keeps_original_assembly_dependency_after_factory_reset(monkeypatch):
    runtime = assemble_runtime(config=runtime_config("in_memory"))
    api = runtime.api
    try:
        leaves = [write_snapshot(api, minute) for minute in (0, 1)]
        security = security_for(api, HOME)
        api.evolve(HOME, **vars(task_options()), security=security)
        Factory.reset_all()

        build = Mock(side_effect=AssertionError("old runtime must retain its embedder"))
        monkeypatch.setattr(EmbedderProducer, "build", build)
        result = api.search("snapshot evidence", Context(HOME), top_k=2, security=security)
        build.assert_not_called()
        assert len(result.items) == 2
        assert result.items[1].unit_id in {leaf.id for leaf in leaves}
    finally:
        runtime.close()
        Factory.reset_all()
