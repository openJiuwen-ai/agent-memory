# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Codec registry — registry of pre-built codec *instances*, keyed by name.

The registry stores already-constructed :class:`StorageCodec` instances, not
factory functions. Construction (including multi-param codecs such as HSM
with cert/slot/pin) is done by the caller at registration time; the engine
only looks an instance up by name and never calls a constructor — so the
"hard-coded ``key=``" limitation of the old factory disappears: any
arbitrary construction signature is supported.

The registry instance is replaceable: tests can construct a fresh
``CodecRegistry()`` for isolation rather than mutating process-global state.
"""
from __future__ import annotations

from typing import Optional

from jiuwen_memory.foundation.codec.storage_codec import StorageCodec


class CodecRegistry:
    """Registry of pre-built codec instances keyed by a stable short name.

    Same-name registration overwrites silently. Not thread-safe by design —
    registration happens at import / startup time, not on the hot path.
    """

    def __init__(self) -> None:
        self._items: dict[str, StorageCodec] = {}

    def register(self, name: str, codec: StorageCodec) -> None:
        """Register a pre-built codec instance under ``name``. Same name overwrites."""
        if not name:
            raise ValueError("codec name must be non-empty")
        self._items[name] = codec

    def get(self, name: str) -> Optional[StorageCodec]:
        """Return the codec instance registered under ``name``, or ``None`` if absent."""
        return self._items.get(name)

    def unregister(self, name: str) -> None:
        self._items.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self._items

    def names(self) -> list[str]:
        """Return registered names (for error messages / debugging)."""
        return list(self._items.keys())


# Process-level default registry instance. Replaceable via
# ``get_default_registry`` consumers building a fresh ``CodecRegistry``.
_default_registry = CodecRegistry()


def get_default_registry() -> CodecRegistry:
    """Return the process-default codec registry.

    Tests that need isolation can construct their own ``CodecRegistry()`` and
    pass instances to it directly rather than mutating this singleton.
    """
    return _default_registry


def register_storage_codec(name: str, codec: StorageCodec) -> None:
    """Module-level convenience for registering a codec on the default registry.

    Open to customers — register a **pre-built** codec instance under a short
    name, then reference that name from ``MemoryEngineConfig.codec``. The
    caller is responsible for constructing (and thus validating) the instance;
    the engine performs only a light ``isinstance(StorageCodec)`` check.

    SM4 (single-param, built at registration)::

        from jiuwen_memory.foundation.codec import register_storage_codec
        register_storage_codec("sm4", SM4StorageCodec(key=sm4_key))

    HSM (multi-param — constructed by the caller, engine never calls a ctor)::

        register_storage_codec("hsm", HSMCodec(cert=..., slot=..., pin=...))
    """
    get_default_registry().register(name, codec)


def unregister_storage_codec(name: str) -> None:
    get_default_registry().unregister(name)
