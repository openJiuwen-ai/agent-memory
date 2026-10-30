# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""本地空间治理与内容权限示例，读取随附完整配置。"""

from _support import kernel_config

from jiuwen_memory.api import (
    Credentials,
    Scope,
    SpaceMember,
    SpaceSpec,
    assemble_server_runtime,
)

runtime = assemble_server_runtime(config=kernel_config("space.yml"))


def security(selector):
    return runtime.authenticate(Credentials(api_key=selector))


try:
    ops, alice, bob = security("demo-ops"), security("demo-alice"), security("demo-bob")
    runtime.api.create_space(
        SpaceSpec(
            org="demo",
            space="team",
            owner=Scope(org="demo", user="alice"),
        ),
        security=ops,
    )
    runtime.api.add_space_member(
        "demo",
        "team",
        SpaceMember(scope=Scope(user="bob")),
        security=alice,
    )
    target = Scope(org="demo", space="team")
    unit = runtime.api.add("Alice uses Python", target, security=alice)[0]
    assert runtime.api.get(unit.id, target, security=bob).content == "Alice uses Python"
finally:
    runtime.close()

print("PASS space: create/member/write/read")
