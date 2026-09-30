# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""truncate_unit_content 护栏单测。

判定类 LLM 调用（分类/抽取/归属/摘要/关联/分层）不需要全文：护栏把超长 unit 内容
截为头 3/4 + 尾 1/4 并标记省略字符数，防止单条超大 unit 把 prompt 撑爆
（BUG2026092802777：500KB 全文进分类 prompt → 3×300s 超时 ≈900s 无响应）。
"""

from jiuwen_memory.construction.prompt_guard import (
    DEFAULT_MAX_UNIT_CHARS,
    truncate_unit_content,
)


def test_short_content_returned_unchanged():
    """未超限内容原样返回（同一对象，零拷贝）。"""
    content = "short content: user prefers Python"
    assert truncate_unit_content(content) is content


def test_exact_limit_returned_unchanged():
    """恰好等于上限的内容不截断（只有超限才截）。"""
    content = "x" * DEFAULT_MAX_UNIT_CHARS
    assert truncate_unit_content(content) is content


def test_over_limit_truncated_head_tail_and_marker():
    """超限内容截为头 3/4 + 尾 1/4，中段以省略标记替代，保留首尾可辨信息。"""
    limit = 400
    content = "HEAD-" + "m" * 2000 + "-TAIL"  # len=2010，明显超限
    result = truncate_unit_content(content, limit=limit)

    head = limit * 3 // 4  # 300
    tail = limit // 4  # 100
    omitted = len(content) - head - tail  # 1610
    assert result == f"{content[:head]}\n...[TRUNCATED {omitted} chars]...\n{content[-tail:]}"
    assert "TRUNCATED" in result
    # 首尾信息保留，整体有界
    assert result.startswith("HEAD-")
    assert result.endswith("-TAIL")
    assert len(result) < len(content)


def test_disabled_when_limit_non_positive():
    """limit <= 0 关闭护栏（显式选择不截断的部署可用）。"""
    content = "x" * 1000
    assert truncate_unit_content(content, limit=0) is content
    assert truncate_unit_content(content, limit=-1) is content
