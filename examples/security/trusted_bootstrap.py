# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""纯 Trusted 配置的受信主体初始化；从仓库根目录执行，无外部网关或 API Key。"""

import logging
import secrets

from jiuwen_memory.api import (
    Credentials,
    PermissionDeniedError,
    Scope,
    assemble_server_runtime,
    register_trusted_principal,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

gateway_key = secrets.token_urlsafe(32)
config = {
    "security": {
        "default": {
            "target": "standard",
            "params": {
                "authenticator": "gateway",
                "authorizer": "default",
            },
        }
    },
    "authenticator": {
        "gateway": {
            "target": "trusted",
            "params": {
                "key_store": "principals",
                "gateway_key": gateway_key,
            },
        }
    },
    "key_store": {"principals": {"target": "memory"}},
    "authorizer": {
        "default": {
            "target": "standard",
            "params": {
                "grant_store": "default",
                "delegation_store": "default",
            },
        }
    },
    "grant_store": {"default": {"target": "memory"}},
    "delegation_store": {"default": {"target": "memory"}},
}
runtime = assemble_server_runtime(config=config)
try:
    # 仅部署初始化代码调用；先完成登记，再开始接收网关请求。
    alice = Scope(org="demo", user="alice")
    register_trusted_principal(runtime.security_runtime.authenticator, alice, role="user")
    security = runtime.authenticate(
        Credentials(
            api_key=gateway_key,
            headers={
                "x-org-id": "demo",
                "x-principal-type": "user",
                "x-principal-id": "alice",
            },
        )
    )
    unit = runtime.api.add("Trusted bootstrap demo", alice, security=security)[0]
    if runtime.api.get(unit.id, alice, security=security).content != "Trusted bootstrap demo":
        raise RuntimeError("Trusted bootstrap read did not return the written content")
    try:
        runtime.api.add("denied", Scope(org="demo", user="bob"), security=security)
    except PermissionDeniedError:
        pass
    else:
        raise RuntimeError("Alice must not write Bob scope")
    logger.info("PASS Trusted bootstrap: owner allowed; other scope denied; no subject API Key")
finally:
    runtime.close(wait=True)
