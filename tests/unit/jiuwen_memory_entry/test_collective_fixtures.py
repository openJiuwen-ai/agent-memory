# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""群体记忆测试辅助函数的失败检查不能依赖可被优化移除的 assert。"""

from unittest.mock import Mock

import pytest

from jiuwen_memory.common.llm.base import LlmProducer
from jiuwen_memory.config.context import AssemblyContext
from tests.integration.jiuwen_memory_entry import fixtures as collective_fixtures

pytestmark = pytest.mark.unit


def test_collective_llm_rejects_missing_source_ids() -> None:
    llm = LlmProducer.build("http_collective_fixture", {}, AssemblyContext())

    with pytest.raises(pytest.fail.Exception, match="must supply source IDs"):
        llm.generate("message without source identifiers")


@pytest.mark.parametrize("failed_method", ["create_space", "add_space_member"])
def test_provision_spaces_stops_on_failed_request(failed_method, monkeypatch) -> None:
    responses = [(200, {})] * 4 if failed_method == "add_space_member" else []
    responses.append((403, {"error": "PermissionDeniedError", "message": "permission denied"}))
    post_stub = Mock(side_effect=responses)
    monkeypatch.setattr(collective_fixtures, "post_as", post_stub)

    with pytest.raises(pytest.fail.Exception, match=failed_method) as failure:
        collective_fixtures.provision_spaces("http://127.0.0.1:8137")

    assert "403" in str(failure.value)
    assert "permission denied" in str(failure.value)
    assert post_stub.call_count == len(responses), "setup must stop at the first failed request"


def test_provision_spaces_completes_all_setup_requests(monkeypatch) -> None:
    post_stub = Mock(return_value=(200, {}))
    monkeypatch.setattr(collective_fixtures, "post_as", post_stub)

    collective_fixtures.provision_spaces("http://127.0.0.1:8137")

    assert [call.args[2] for call in post_stub.call_args_list] == (
        ["create_space"] * 4 + ["add_space_member"] * 2
    )
