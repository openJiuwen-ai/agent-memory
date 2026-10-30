# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SDK Grant 完整生命周期，显式配置认证、授权与内存 Store。"""

from _support import kernel_config, logger

from jiuwen_memory.api import (
    Action,
    Credentials,
    Grant,
    PermissionDeniedError,
    Scope,
    assemble_server_runtime,
)

config = kernel_config("grant-dev.yml")
runtime = assemble_server_runtime(config=config)


def security(selector):
    return runtime.authenticate(Credentials(api_key=selector))


try:
    alice, bob = security("demo-alice"), security("demo-bob")
    target = Scope(org="demo", user="alice")
    unit = runtime.api.add("Alice uses Python", target, security=alice)[0]
    try:
        runtime.api.get(unit.id, target, security=bob)
    except PermissionDeniedError:
        pass
    else:
        raise RuntimeError("Bob must not read before a grant")
    granted = runtime.api.grant(
        Grant(
            grantor=target,
            grantee=bob.auth.actor,
            actions=frozenset({Action.READ}),
        ),
        security=alice,
    )
    if not granted.grant_id:
        raise RuntimeError("Grant creation must return a grant ID")
    if runtime.api.get(unit.id, target, security=bob).content != "Alice uses Python":
        raise RuntimeError("Bob must read the content after the Grant")
    runtime.api.revoke(granted, security=alice)
    try:
        runtime.api.get(unit.id, target, security=bob)
    except PermissionDeniedError:
        pass
    else:
        raise RuntimeError("Bob must not read after revocation")
finally:
    runtime.close()

logger.info("PASS SDK Grant: denied/allowed/revoked")
