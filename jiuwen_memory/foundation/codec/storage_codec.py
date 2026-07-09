# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Storage codec interface contract — the single source of truth.

:class:`StorageCodec` is a store-layer contract: it is eagerly imported by
several ``foundation.store.index`` modules, so its definition lives here in
``foundation.codec`` (not under ``memory_core``). Keeping it out of the
``memory_core`` package avoids a circular import — importing a name from
``memory_core`` would trigger ``jiuwen_memory.memory_core.__init__`` (a heavy
chain that imports back into ``foundation.store``).

Custom codec implementations (SM4, HSM-backed, ...) only implement
``encode`` / ``decode``; instances are built by the caller, registered into
:class:`CodecRegistry` under a short name, looked up by name in
``LongTermMemory.set_config`` and injected into each consumer via
``set_storage_codec`` / ``set_codec``.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageCodec(Protocol):
    """Storage-layer encryption/decryption contract.

    Implementations transparently encrypt/decrypt the ``text`` field of memory
    documents (and similar string payloads such as message content, variable
    values, scope ``api_key``). Both explicit inheritance and duck typing are
    accepted; ``@runtime_checkable`` makes ``isinstance(x, StorageCodec)``
    usable for runtime validation when a registered instance is looked up.

    Constraints (must hold for any conforming implementation):
    - ``encode(plaintext) -> ciphertext``
    - ``decode(ciphertext) -> plaintext``
    - ``encode(None/empty) -> original value`` (pass-through, matching
      :class:`AesStorageCodec` behavior so an empty key disables crypto)
    - ``decode(None/empty) -> original value``
    - ``decode(encode(x)) == x``

    .. code-block:: python

        class AesStorageCodec:
            def __init__(self, key: bytes):
                self._key = key

            def encode(self, text: str) -> str:
                return aes_encrypt(text)

            def decode(self, data: str) -> str:
                return aes_decrypt(data)

        index = SimpleMemoryIndex(...)
        index.set_storage_codec(AesStorageCodec(key=b"..."))
    """

    def encode(self, text: str) -> str:
        """Encode a plaintext string into a (possibly encrypted) string."""
        ...

    def decode(self, data: str) -> str:
        """Decode a (possibly encrypted) string back into plaintext."""
        ...
