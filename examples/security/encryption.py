# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一开关下的 DEV USER API 写入/检索；应用加密验证，不代替磁盘部署验收。"""

import os
import secrets

from _support import kernel_config

from jiuwen_memory.api import Context, Credentials, DisclosureLevel, Scope, assemble_server_runtime

os.environ["AGENT_MEMORY_ENCRYPTION_ROOT_KEY"] = secrets.token_hex(32)
runtime = assemble_server_runtime(config=kernel_config("encryption.yml"))
try:
    security = runtime.authenticate(Credentials())
    scope = Scope(org="local", user="developer")
    units = runtime.api.add("project zephyr launch plan", scope, security=security)
    assert (
        runtime.api.get(units[0].id, scope, security=security).content
        == "project zephyr launch plan"
    )
    result = runtime.api.search(
        "zephyr",
        Context(scope=scope),
        disclosure=DisclosureLevel.L2,
        security=security,
    )
    assert units and result.items
    print("PASS encrypted API: write/read/search as DEV USER")
finally:
    runtime.close(wait=True)
