# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""split_within_limit 段落限长切分单测（ingest 预切分与 construction 内容分批共用实现）。

语义约定：结果以 "\n\n" join 严格还原原文（无损可逆）；limit <= 0 或文本未超限
返回单片段；单段超长时硬切兜底，硬切点可能引入一个段落分隔（不影响记忆语义）。
"""

import pytest

from jiuwen_memory.common.text_split import split_within_limit

pytestmark = pytest.mark.unit


def test_within_limit_returns_single_piece() -> None:
    text = "short text"
    assert split_within_limit(text, 100) == [text]


def test_non_positive_limit_disables_split() -> None:
    text = "y" * 500
    assert split_within_limit(text, 0) == [text]
    assert split_within_limit(text, -1) == [text]


def test_paragraph_aggregation_reversible() -> None:
    paras = [f"para{i} " + "x" * 40 for i in range(10)]  # 每段 46 chars
    text = "\n\n".join(paras)  # 478 chars
    pieces = split_within_limit(text, 200)

    assert len(pieces) > 1
    assert all(len(p) <= 200 for p in pieces)
    assert "\n\n".join(pieces) == text  # 无损可逆


def test_hard_cut_oversized_single_paragraph_reversible() -> None:
    text = "y" * 500
    pieces = split_within_limit(text, 200)

    assert len(pieces) == 3
    assert all(len(p) <= 200 for p in pieces)
    assert "".join(pieces) == text


def test_leading_separator_preserved() -> None:
    """首部空段落（前导分隔符）不因切分丢失。"""
    text = "\n\n" + "\n\n".join(f"para{i} " + "x" * 40 for i in range(6))
    pieces = split_within_limit(text, 200)

    assert len(pieces) > 1
    assert "\n\n".join(pieces) == text


def test_empty_paragraph_at_flush_boundary_preserved() -> None:
    """空段落恰在缓冲冲刷边界也不丢中间分隔符。"""
    text = "aaaa\n\n" + "b" * 20 + "\n\n\n\ncc"
    pieces = split_within_limit(text, 20)

    assert len(pieces) > 1
    assert "\n\n".join(pieces) == text
