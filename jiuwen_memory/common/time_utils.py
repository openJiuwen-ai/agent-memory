# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""记忆时间的公共解释规则：不依赖进程本地时区，不改写输入值。"""

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    """无时区时间按 UTC 解释；有时区时间保留实际时刻，比较保留微秒。"""
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def epoch_ms(value: datetime) -> int:
    """按公共时间规则投影为 epoch 毫秒；缺值与哨兵由调用方处理。"""
    return int(as_utc(value).timestamp() * 1000)
