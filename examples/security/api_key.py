# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""按完整配置切换 DEV/API Key，在实际 HTTP 服务内校验 Root 并签发 USER Key。"""

import os
import secrets

from _support import post, running

from jiuwen_memory.api import Credentials, Scope
from jiuwen_memory.common.security.types import Role

with running("dev.yml") as (_, base):
    post(
        base,
        "add",
        None,
        {
            "content": "DEV test",
            "scope": {"org": "local", "user": "developer"},
        },
    )
    print("PASS DEV: no credential -> 200")

# 只改变本示例进程的环境；不打印 Key，也不改调用终端的变量。
os.environ["AGENT_MEMORY_ROOT_API_KEY"] = secrets.token_urlsafe(32)
with running("api-key.yml") as (service, base):
    root_key = os.environ["AGENT_MEMORY_ROOT_API_KEY"]
    root_body = {"content": "Root test", "scope": {"org": "system", "user": "root"}}
    post(base, "add", None, root_body, 401)
    post(base, "add", "invalid-test-key", root_body, 401)
    os.environ["AGENT_MEMORY_API_KEY"] = root_key
    post(base, "add", os.environ["AGENT_MEMORY_API_KEY"], root_body)
    print("PASS API Key: missing/wrong -> 401; Root -> 200")

    # 受信管理代码必须校验 Root，并使用实际服务的同一个 KeyStore。
    admin = service.authenticator.authenticate(Credentials(api_key=root_key))
    if admin.role is not Role.ROOT:
        raise PermissionError("ROOT management identity required")
    alice = Scope(org="demo", user="alice")
    alice_key = service.authenticator.key_store.issue(alice, Role.USER)
    assert alice_key != root_key
    os.environ["AGENT_MEMORY_API_KEY"] = alice_key
    identity = service.authenticator.authenticate(Credentials(api_key=alice_key))
    assert identity.actor == alice and identity.role is Role.USER
    print("PASS issuance: Root checked; independent USER Key issued")

    body = {"content": "Alice private demo", "scope": {"org": "demo", "user": "alice"}}
    units = post(base, "add", os.environ["AGENT_MEMORY_API_KEY"], body)
    unit = post(base, "get", alice_key, {"unit_id": units[0]["id"], "scope": body["scope"]})
    assert unit["segments"][0]["content"] == "Alice private demo"
    post(base, "add", alice_key, root_body, 403)
    print("PASS USER HTTP: own write/read -> 200; Root scope -> 403")
print("PASS complete: listeners closed; in-memory Keys discarded")
