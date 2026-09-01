# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""两个 MemoryEngine 实现共用的中期记忆 write 辅助。"""

from __future__ import annotations

from typing import Any

from jiuwen_memory.common.errors import ValidationError


def parse_middle_interval(value: Any) -> int | None:
    """解析 write metadata 的 ``middle_interval``。

    ``None`` 表示缺省，由 ``MiddleToLongJobSpec.interval`` 装配期默认兜底。
    非 ``None`` 时必须能转为正整数，否则在落盘前 fail fast。
    """
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "system_metadata[middle_interval] 必须是正整数"
        ) from exc
    if parsed <= 0:
        raise ValidationError("system_metadata[middle_interval] 必须 > 0")
    return parsed
