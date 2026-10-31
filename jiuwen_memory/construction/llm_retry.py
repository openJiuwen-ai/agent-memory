# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""构建层 LLM 调用的统一重试入口。

超时是确定性失败（prompt 过大或后端饱和），原样重试只会把等待时间叠加成
N×timeout（BUG2026092802777：实测 3×300s=900s 无响应）——因此对 LLM 契约的
统一超时类型 LLMTimeoutError（由各适配器转换，见 common/llm/base.py 的 chat
契约）与内建 TimeoutError 直接抛出，不重试；其余异常按指数退避重试至
max_attempts。各算子的 ``_call_llm_with_retry`` 一律委托本入口，不再各自复制
重试循环，也不感知具体 SDK 异常。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from jiuwen_memory.common.errors import LLMTimeoutError


def is_timeout_error(exc: BaseException) -> bool:
    """契约判定：LLMTimeoutError（适配器转换后）或内建 TimeoutError 子类。

    ConnectTimeout 等连接建立超时应由适配器自行取舍（可能是瞬时抖动，
    保留重试语义），不在此兜底。
    """
    return isinstance(exc, LLMTimeoutError) or isinstance(exc, TimeoutError)


def call_llm_with_retry(
    chat: Callable[..., str],
    messages: list,
    *,
    max_attempts: int,
    backoff_ms: int,
    logger: logging.Logger,
    label: str,
    **options: Any,
) -> str:
    """执行一次 chat 调用：非超时异常按指数退避重试，超时直接抛出。"""
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return chat(messages, **options)
        except Exception as exc:
            if is_timeout_error(exc):
                raise
            last_exc = exc
            if attempt == max_attempts - 1:
                break
            wait = backoff_ms * (2**attempt) / 1000.0
            logger.warning(
                "%s: LLM call failed (attempt %d), retrying in %.1fs",
                label,
                attempt + 1,
                wait,
            )
            time.sleep(wait)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{label}: LLM call did not execute")
