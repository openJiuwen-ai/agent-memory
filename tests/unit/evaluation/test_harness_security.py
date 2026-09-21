# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""本地评测身份不能由数据集的目标 Scope 决定。"""

import pytest

from evaluation.longmemeval.harness import EvalHarness
from jiuwen_memory.api import Scope

pytestmark = pytest.mark.unit


def test_harness_uses_fixed_dev_identity_for_different_targets() -> None:
    # 白盒回归需验证私有装配真源/故障注入；不为测试扩充公共接口。
    # pylint: disable=protected-access
    harness = EvalHarness()
    try:
        first = harness._security_kwargs(harness._api.add, Scope(org="one", user="alice"))
        second = harness._security_kwargs(harness._api.add, Scope(org="two", user="bob"))
        assert first["security"].auth.actor == Scope(org="local", user="developer")
        assert second["security"].auth.actor == first["security"].auth.actor
        assert first["security"].request_id != second["security"].request_id
        assert first["security"].has_valid_origin()
        target = Scope(org="one", user="alice")
        unit = harness._api.add("evaluation sample", target, **first)[0]
        assert unit.system_metadata["author_principal"] == "user:developer"
    finally:
        harness.close()
