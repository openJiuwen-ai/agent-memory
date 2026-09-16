# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pure projection of Schema property provenance; no lifecycle/time filtering."""

from jiuwen_memory.common.memory_sources import sources
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX
from jiuwen_memory.common.type_def.memory_codec import loads


def property_sources(key: str, raw: bytes) -> set[str]:
    """Non-memory records are not indexed. Invalid memory encoding fails the write."""
    if not key.startswith(MEMORY_KEY_PREFIX):
        return set()
    unit = loads(raw)
    if unit is None or unit.system_metadata.get("extraction_mode") != "schema":
        return set()
    return set(sources(unit))


class MemorySourceIndex:
    """One Scope's relation sets; the owning KV store supplies the lock."""

    def __init__(self) -> None:
        self.by_source: dict[str, set[str]] = {}
        self.by_property: dict[str, set[str]] = {}

    def replace(self, key: str, new_sources: set[str]) -> None:
        previous = self.by_property.get(key, set())
        for source in previous - new_sources:
            members = self.by_source[source]
            members.discard(key)
            if not members:
                del self.by_source[source]
        for source in new_sources - previous:
            self.by_source.setdefault(source, set()).add(key)
        if new_sources:
            self.by_property[key] = new_sources
        else:
            self.by_property.pop(key, None)
