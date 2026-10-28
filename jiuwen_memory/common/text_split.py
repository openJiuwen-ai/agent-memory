# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""按段落边界的限长切分：ingest 预切分（SimpleIngestor._split）与 construction
内容型任务的完整分批（prompt_guard.chunk_unit_content）共用同一份实现。

语义约定（两侧一致）：段落聚合路径无损可逆，``"\n\n".join(pieces)`` 严格还原
原文（含首部空段落与冲刷边界上的空段落分隔符）；单段超长时硬切兜底，硬切点
拼接会引入一个段落分隔（不影响记忆语义）；limit <= 0 或文本未超限时返回单片段。
"""

from __future__ import annotations


def split_within_limit(content: str, limit: int) -> list[str]:
    """按段落边界聚合为 <= limit 的片段（无信息丢失，可逆拼接）。"""
    if limit <= 0 or len(content) <= limit:
        return [content]
    pieces: list[str] = []
    buf: str | None = None  # None=尚未缓冲段落；"" 是合法缓冲（空段落）
    for para in content.split("\n\n"):
        candidate = para if buf is None else f"{buf}\n\n{para}"
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf is not None:
            pieces.append(buf)
            buf = None
        # while 用严格 > 截切，残段恒非空；para 本身可为空段落（空串），
        # 原样缓冲以保留分隔符。
        while len(para) > limit:
            pieces.append(para[:limit])
            para = para[limit:]
        buf = para
    if buf is not None:
        pieces.append(buf)
    return pieces
