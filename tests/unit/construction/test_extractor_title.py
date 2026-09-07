"""LLM 抽取器新增的 md 块标题（F08 §8）——解析清洗与落盘。

``_parse_title`` 与 ``_parse_procedural_response`` 把 LLM 产出的 ``title`` 字段清洗为
「单行 ≤50 字符」后落 ``unit.system_metadata[MD_TITLE_KEY]``，供 md 渲染标题行。失效方向：

- 标题含换行未折叠 → md 块被切碎、看门狗按行遍历把第 2+ 行当幽灵 unit。
- 标题超长未截断 → 块格式破坏（标题行与正文行不再一一对应）。
- 空/非法标题未兜底为空串 → 落盘侧无法回退 ``# {unit.id}`` 占位。

本文件测解析函数与标题落到派生 unit 的完整链路（``ExtractTarget`` 主路 + procedural 路）。
"""

from __future__ import annotations

# pylint: disable=protected-access  # 测试直取内部装配与状态以断言接线行为

import json

import pytest

from jiuwen_memory.common.type_def import MD_TITLE_KEY, MemoryUnit, Scope, Segment
from jiuwen_memory.construction.extractor_impl.llm_extractor import (
    ExtractorImpl,
    _parse_title,
)
from tests.unit.construction.fixtures import MockLLM

pytestmark = pytest.mark.unit


# -- _parse_title ------------------------------------------------------------ #


def test_parse_title_strips_whitespace() -> None:
    assert _parse_title("  Frontend framework  ") == "Frontend framework"


def test_parse_title_collapses_inner_newlines_to_single_line() -> None:
    """标题含换行折叠成单空格——块格式要求标题恒单行。"""
    assert _parse_title("Frontend\nframework\nchoice") == "Frontend framework choice"


def test_parse_title_truncates_to_fifty_chars() -> None:
    assert len(_parse_title("x" * 100)) == 50


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_parse_title_falls_back_to_empty_on_empty_input(raw: object) -> None:
    """空串/空白/None 容错为空串——落盘侧兜底 ``# {unit.id}``。"""
    assert _parse_title(raw) == ""


def test_parse_title_strifies_truthy_non_string_input() -> None:
    """非 str 且 truthy 的输入被 ``str(raw or "")`` 强制转换（实现事实）。

    LLM 正常不会输出 int/list，此处锁定实现行为，防未来改动误判为「应返空串」。
    """
    assert _parse_title(123) == "123"
    assert _parse_title(["a"]) == "['a']"


# -- _parse_procedural_response --------------------------------------------- #


def _extractor() -> ExtractorImpl:
    return ExtractorImpl(
        llm=MockLLM(),
        min_confidence=0.0,
        retry_max_retries=1,
        retry_backoff_ms=1,
    )


def test_parse_procedural_response_extracts_content_and_title() -> None:
    content, title = _extractor()._parse_procedural_response(
        '{"content": "deployed service", "title": "Deploy done"}'
    )
    assert content == "deployed service"
    assert title == "Deploy done"


def test_parse_procedural_response_strips_markdown_fence() -> None:
    content, title = _extractor()._parse_procedural_response(
        '```json\n{"content": "x", "title": "Y"}\n```'
    )
    assert (content, title) == ("x", "Y")


def test_parse_procedural_response_uses_raw_as_content_when_unparsable() -> None:
    content, title = _extractor()._parse_procedural_response("plain text, no json")
    assert content == "plain text, no json"
    assert title == ""


# -- title 落盘（派生 unit） -------------------------------------------------- #


def _source(uid: str, content: str) -> MemoryUnit:
    return MemoryUnit(
        id=uid,
        scope=Scope(org="test", user="alice"),
        segments=[Segment(content=content)],
        system_metadata={"infer": "true"},
    )


def test_extracted_title_lands_in_system_metadata() -> None:
    extractor = ExtractorImpl(
        llm=MockLLM(
            responses=[
                json.dumps(
                    [
                        {
                            "source_id": "u1",
                            "target": "fact",
                            "tier": "semantic",
                            "content": "Alice likes coffee",
                            "title": "Coffee preference",
                            "confidence": 1.0,
                        }
                    ]
                )
            ]
        ),
        min_confidence=0.0,
        retry_max_retries=1,
        retry_backoff_ms=1,
    )

    (derived,) = extractor.extract([_source("u1", "Alice 说她喜欢喝咖啡")])

    assert derived.system_metadata[MD_TITLE_KEY] == "Coffee preference"


def test_no_title_leaves_metadata_absent() -> None:
    """LLM 未产 title → 不写 MD_TITLE_KEY，落盘侧兜底 unit.id。"""
    extractor = ExtractorImpl(
        llm=MockLLM(
            responses=[
                json.dumps(
                    [
                        {
                            "source_id": "u1",
                            "target": "fact",
                            "tier": "semantic",
                            "content": "Alice likes coffee",
                            "confidence": 1.0,
                        }
                    ]
                )
            ]
        ),
        min_confidence=0.0,
        retry_max_retries=1,
        retry_backoff_ms=1,
    )

    (derived,) = extractor.extract([_source("u1", "Alice 说她喜欢喝咖啡")])

    assert MD_TITLE_KEY not in derived.system_metadata
