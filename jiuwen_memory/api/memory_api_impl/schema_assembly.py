"""Opt-in assembly wrappers for Schema-enabled kernels."""

from __future__ import annotations

from jiuwen_memory.api.memory_api_impl.assembly import Kernel, build_kernel
from jiuwen_memory.config import Config
from jiuwen_memory.construction.schema_bootstrap import register_schema_constructors
from jiuwen_memory.storage.kv import KVStore
from jiuwen_memory.storage.schema_bootstrap import register_schema_storage


def build_schema_kernel(
    policies: dict[str, str] | None = None,
    kv: KVStore | None = None,
    config: Config | None = None,
) -> Kernel:
    """Register Schema targets, then delegate all official assembly to ``build_kernel``."""

    register_schema_storage()
    register_schema_constructors()
    return build_kernel(policies=policies, kv=kv, config=config)


def assemble_schema(
    policies: dict[str, str] | None = None,
    kv: KVStore | None = None,
    config: Config | None = None,
):
    """Return the Schema-enabled local MemoryAPI."""

    return build_schema_kernel(policies=policies, kv=kv, config=config).api
