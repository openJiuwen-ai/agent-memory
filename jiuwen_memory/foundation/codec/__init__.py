# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Storage codec package — the single extension point for custom encryption.

Public API:
    StorageCodec           : storage-layer encode/decode protocol (duck-typed)
    AesStorageCodec        : built-in AES-256-GCM default implementation
    CodecRegistry          : replaceable registry of pre-built codec instances
    get_default_registry   : accessor for the process-default registry
    register_storage_codec : one-liner registration on the default registry

Customer integration — the registry stores *pre-built instances*, so any
construction signature is supported (single-param SM4 or multi-param HSM).
The engine looks an instance up by name and never calls a constructor::

    from jiuwen_memory.foundation.codec import StorageCodec, register_storage_codec

    class SM4StorageCodec:
        def __init__(self, key: bytes): self._key = key
        def encode(self, text: str) -> str: ...
        def decode(self, data: str) -> str: ...

    # construct (any signature) then register the instance
    register_storage_codec("sm4", SM4StorageCodec(key=sm4_key))

    config = MemoryEngineConfig(crypto_key=sm4_key, codec="sm4")

If ``config.codec`` is empty, or no instance is registered under that name,
the engine falls back to ``AesStorageCodec(config.crypto_key)``.

"""
from jiuwen_memory.foundation.codec.aes_storage_codec import AesStorageCodec
from jiuwen_memory.foundation.codec.sm4_storage_codec import SM4StorageCodec
from jiuwen_memory.foundation.codec.codec_registry import (
    CodecRegistry,
    get_default_registry,
    register_storage_codec,
    unregister_storage_codec,
)
from jiuwen_memory.foundation.codec.storage_codec import StorageCodec

__all__ = [
    "StorageCodec",
    "AesStorageCodec",
    "SM4StorageCodec",
    "CodecRegistry",
    "get_default_registry",
    "register_storage_codec",
    "unregister_storage_codec",
]

