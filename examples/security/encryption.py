# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""验证真实 API 数据的 ENC1 密文与篡改拒绝；不代替磁盘部署验收。"""

import os
import secrets

from _support import kernel_config, logger

from jiuwen_memory.api import Context, Credentials, DisclosureLevel, Scope, assemble_server_runtime
from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import memory_key
from jiuwen_memory.storage.store_manager import StorageCapability, StoreManager


def verify_encryption(config):
    built = []
    # 仅测试装配时观察公开对象，取得同一 API 实际使用的 Store；不新增 raw 具名端口。
    with Factory.observe_builds(built.append):
        runtime = assemble_server_runtime(config=config)
    try:
        security = runtime.authenticate(Credentials())
        scope = Scope(org="local", user="developer")
        content = "project zephyr launch plan"
        units = runtime.api.add(content, scope, security=security)
        if not units:
            raise RuntimeError("Encrypted write must return a memory unit")
        if runtime.api.get(units[0].id, scope, security=security).content != content:
            raise RuntimeError("Encrypted read must return the written content")
        result = runtime.api.search(
            "zephyr",
            Context(scope=scope),
            disclosure=DisclosureLevel.L2,
            security=security,
        )
        if not result.items:
            raise RuntimeError("Encrypted search must return results")
        logger.info("PASS encrypted API: write/read/search as DEV USER")

        managers = [instance for instance in built if isinstance(instance, StoreManager)]
        if len(managers) != 1:
            raise RuntimeError("Expected one StoreManager for the tested API")
        manager = managers[0]
        if set(manager.encryption_inventory()[StorageCapability.KV]) != {"default"}:
            raise RuntimeError("Example must expose only the default KV port, without raw ports")
        kv = manager.kv()
        binding = kv.security.encryption_binding
        if binding is None:
            raise RuntimeError("Expected ENC1 encryption binding; encryption is not enabled")
        raw = binding.inner
        key = memory_key(units[0].id)
        ciphertext = raw.get(scope, key)
        plaintext = kv.get(scope, key)
        if not isinstance(ciphertext, bytes) or not ciphertext.startswith(b"ENC1"):
            raise RuntimeError("Raw KV value must be an ENC1 ciphertext")
        if ciphertext == plaintext or content.encode("utf-8") in ciphertext:
            raise RuntimeError("Raw KV value must differ from plaintext and hide the content")
        logger.info("PASS encrypted KV: ENC1 ciphertext differs from plaintext")

        # 仅损坏本进程临时内存中的密文末字节，保留信封头；模拟正文/认证标签被篡改。
        raw.update(scope, key, ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
        try:
            try:
                runtime.api.get(units[0].id, scope, security=security)
            except BackendError:
                pass
            else:
                raise RuntimeError("API read must reject tampered ciphertext")
        finally:
            raw.update(scope, key, ciphertext)
        if runtime.api.get(units[0].id, scope, security=security).content != content:
            raise RuntimeError("Restoring the ciphertext must restore the API read")
        logger.info("PASS encrypted integrity: tampered ciphertext rejected; original restored")
    finally:
        runtime.close(wait=True)


if __name__ == "__main__":
    os.environ["AGENT_MEMORY_ENCRYPTION_ROOT_KEY"] = secrets.token_hex(32)
    verify_encryption(kernel_config("encryption.yml"))
