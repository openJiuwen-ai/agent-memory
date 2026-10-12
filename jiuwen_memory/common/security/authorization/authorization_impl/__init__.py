# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Authorizer 实现包：import 各实现模块触发 ``@AuthorizationProducer.register`` 注册。

Grant/Delegation 的存储实现（``memory_stores``、``sqlite_stores``）也在这里 import：
Authorizer 的 ``_build`` 通过 ``GrantStoreProducer.dep`` 按注册名取存储，模块没被
import 过就等于那个名字不存在。注册是声明式的、``dep`` 在 ``_build`` 时才解析，所以
本文件内的 import 次序不影响可用性。

``allow_all`` 是恒放行、``is_test_only()`` 为真的测试专用实现（见
:mod:`.allow_all_authorizer`）；做真实判定的是 :mod:`.standard_authorizer`。
"""

from jiuwen_memory.common.security.authorization.authorization_impl import (  # noqa: F401
    allow_all_authorizer,
    memory_stores,
    routing_authorizer,
    space_aware_authorizer,
    sqlite_stores,
    standard_authorizer,
)
