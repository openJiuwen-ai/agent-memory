# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""主体 Key 撤销后，已认证的请求上下文也必须拒绝继续读取。"""

import os
import secrets

from _support import kernel_config, logger

from jiuwen_memory.api import (
    Credentials,
    PermissionDeniedError,
    Scope,
    assemble_server_runtime,
)
from jiuwen_memory.common.security.authentication.key_store import fingerprint
from jiuwen_memory.common.security.types import Role

os.environ["AGENT_MEMORY_ROOT_API_KEY"] = secrets.token_urlsafe(32)
config = kernel_config("api-key.yml")
runtime = assemble_server_runtime(config=config)
try:
    authenticator = runtime.security_runtime.authenticator
    actor = Scope(org="demo", user="alice")
    token = authenticator.key_store.issue(actor, Role.USER)
    security = runtime.authenticate(Credentials(api_key=token))
    unit = runtime.api.add("private demo", actor, security=security)[0]
    authenticator.key_store.revoke(fingerprint(token))
    try:
        runtime.api.get(unit.id, actor, security=security)
    except PermissionDeniedError:
        pass
    else:
        raise RuntimeError("cached context must fail after the key is revoked")
finally:
    runtime.close()

logger.info("PASS cached Key context revoked")
