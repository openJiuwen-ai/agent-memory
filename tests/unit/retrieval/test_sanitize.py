"""Query sanitize 的纯函数单测。"""

from __future__ import annotations

import pytest

from jiuwen_memory.retrieval.query_parser_impl.sanitize import sanitize_query

pytestmark = pytest.mark.unit


def test_strips_bracket_timestamp() -> None:
    result = sanitize_query("[Fri 2026-03-27 06:16 UTC] 北京天气")

    assert result == "北京天气", "应剥除 UTC 方括号时间戳"


def test_strips_sender_line() -> None:
    result = sanitize_query("Sender (untrusted metadata): openJiuwen-bot\n北京天气")

    assert result == "北京天气", "应整行剥除 sender 元数据"


def test_collapses_whitespace() -> None:
    result = sanitize_query("  北京\n\n   天气\t  怎么样  ")

    assert result == "北京 天气 怎么样", "应折叠连续空白并去除首尾空白"


def test_clean_query_unchanged() -> None:
    result = sanitize_query("如何使用 mem0 的图记忆")

    assert result == "如何使用 mem0 的图记忆", "干净 query 不应被改写"


def test_code_fence_kept_by_default() -> None:
    code_block = "```python\nif x:\n    print(x)\n```"
    result = sanitize_query(f"  查看\n{code_block}\n 的问题 ")

    assert code_block in result, "默认应保留围栏代码块及其内部格式"
    assert result == f"查看 {code_block} 的问题", "只应折叠围栏外空白"


def test_code_fence_stripped_when_enabled() -> None:
    result = sanitize_query("查看 ```python\nprint(1)\n``` 的问题", strip_code_fences=True)

    assert result == "查看 的问题", "开启 strip_code_fences 时应剥除整段围栏代码"


def test_empty_and_none() -> None:
    assert sanitize_query("") == "", "空字符串应返回空串"
    assert sanitize_query(None) == "", "None 应返回空串"
