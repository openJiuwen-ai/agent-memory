# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DEV 默认 USER 的进程内写入、读取和检索，无需模型或数据库。"""

from _support import kernel_config, logger

from jiuwen_memory.api import (
    Context,
    Credentials,
    Scope,
    assemble_server_runtime,
)

config = kernel_config("dev.yml")
runtime = assemble_server_runtime(config=config)
try:
    security = runtime.authenticate(Credentials())
    target = Scope(org="local", user="developer")
    unit = runtime.api.add("local security demo", target, security=security)[0]
    if runtime.api.get(unit.id, target, security=security).content != "local security demo":
        raise RuntimeError("SDK read must return the written content")
    result = runtime.api.search(
        "security demo",
        Context(scope=target),
        security=security,
        with_trajectory=True,
    )
    if result is None:
        raise RuntimeError("SDK search must return a result")
finally:
    runtime.close(wait=True)

logger.info("PASS SDK quickstart: write/read/search")
