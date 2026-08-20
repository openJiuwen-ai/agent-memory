"""Public opt-in entrypoint for Entity Schema extraction."""

from jiuwen_memory.api.memory_api_impl.schema_assembly import (
    assemble_schema,
    build_schema_kernel,
)
from jiuwen_memory.construction.schema_bootstrap import register_schema_constructors
from jiuwen_memory.storage.schema_bootstrap import register_schema_storage

__all__ = [
    "assemble_schema",
    "build_schema_kernel",
    "register_schema_constructors",
    "register_schema_storage",
]
