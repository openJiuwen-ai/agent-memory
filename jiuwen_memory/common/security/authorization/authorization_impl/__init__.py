# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Authorizer 实现包：import 各实现模块触发 ``@AuthorizationProducer.register`` 注册。

PR1 只提供 ``allow_all``——一个恒放行、``is_test_only()`` 为真的装配占位（见
:mod:`.allow_all_authorizer`）。真正做判定的 ``StandardAuthorizer`` 随 PR2 合入。
"""

from jiuwen_memory.common.security.authorization.authorization_impl import (  # noqa: F401
    allow_all_authorizer,
)
