# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""truncate_unit_content 护栏单测。

判定类 LLM 调用（分类/抽取/归属/摘要/关联/分层）不需要全文：护栏把超长 unit 内容
截为头 3/4 + 尾 1/4 并标记省略字符数，防止单条超大 unit 把 prompt 撑爆
（BUG2026092802777：500KB 全文进分类 prompt → 3×300s 超时 ≈900s 无响应）。
"""

from jiuwen_memory.construction.prompt_guard import (
    CONTENT_MAX_UNIT_CHARS,
    DEFAULT_MAX_UNIT_CHARS,
    chunk_unit,
    chunk_unit_content,
    truncate_unit_content,
)
from tests.unit.construction.fixtures import create_test_unit


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


# ---------------------------------------------------------------------------
# 内容型任务无损分批（检视 P2：截断会静默漏掉中段事实）
# ---------------------------------------------------------------------------


def test_chunk_unit_content_single_batch_within_limit():
    """未超限单批原样返回。"""
    content = "short content"
    assert chunk_unit_content(content) == [content]


def test_chunk_unit_content_lossless_over_limit():
    """超限按段落聚合切批：每批 <= limit，join 严格还原原文。"""
    paras = [f"para{i} " + "x" * 40 for i in range(10)]
    content = "\n\n".join(paras)  # 478 chars
    chunks = chunk_unit_content(content, limit=200)

    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert "\n\n".join(chunks) == content  # 无损


def test_chunk_unit_content_default_limit_is_content_max():
    """默认上限即 CONTENT_MAX_UNIT_CHARS，与 ingest 预切分对齐。"""
    content = "x" * (CONTENT_MAX_UNIT_CHARS + 1)
    chunks = chunk_unit_content(content)

    assert len(chunks) == 2
    assert all(len(c) <= CONTENT_MAX_UNIT_CHARS for c in chunks)
    assert "".join(chunks) == content  # 硬切兜底同样无损


def test_chunk_unit_short_returns_same_object():
    """未超限返回原 unit（不拷贝，调用方可直接复用）。"""
    unit = create_test_unit("u1", "short")
    assert chunk_unit(unit) == [unit]
    assert chunk_unit(unit)[0] is unit


def test_chunk_unit_oversized_returns_chunked_copies():
    """超限返回分片副本：id/元数据保留，内容 join 严格还原原文。"""
    head, tail = "甲" * 100, "乙" * 100
    content = head + "\n\n" + tail  # 202 chars
    unit = create_test_unit("u1", content)
    copies = chunk_unit(unit, limit=200)

    assert len(copies) == 2
    assert all(c.id == "u1" for c in copies)
    assert [c.content for c in copies] == [head, tail]
    assert copies[0].system_metadata == unit.system_metadata
