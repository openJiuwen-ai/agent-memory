# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Source identity shared by Schema reconciliation and storage projections."""

from jiuwen_memory.common.type_def import MemoryUnit


def sources(unit: MemoryUnit) -> list[str]:
    """Use all provenance IDs; source_ref is only a fallback, not an extra source."""
    return list(dict.fromkeys(unit.provenance or ([unit.source_ref] if unit.source_ref else [])))
