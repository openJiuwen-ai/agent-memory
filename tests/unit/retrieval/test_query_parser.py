"""Query parser contract tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def.filter import FilterClause, FilterOp
from jiuwen_memory.retrieval.query_parser_impl.simple_query_parser import SimpleQueryParser
from jiuwen_memory.retrieval.types import RecallChannel, RetrievalQuery

pytestmark = pytest.mark.unit


class _RewriteLLM(LLM):
    """测试用 LLM 桩：在输入文本末尾追加标记，便于断言改写是否发生。"""

    def plugin_type(self) -> PluginType:
        return PluginType.LLM

    def health(self) -> None:
        return None

    def chat(self, messages, **options):  # type: ignore[override]
        return messages[-1].content + " [rewritten]"


def test_parser_transfers_filters_and_as_of(world) -> None:
    as_of = datetime(2026, 6, 10, tzinfo=timezone.utc)
    filters = [FilterClause("tags", FilterOp.CONTAINS, "x")]

    query = RetrievalQuery(text="hello world", filters=filters, as_of=as_of)
    parsed = world.parser.parse(query)

    assert parsed.scalar_filters is query.filters
    assert parsed.as_of == as_of
    assert parsed.tokens == ["hello", "world"]
    assert parsed.vector
    assert RecallChannel.KEYWORD in parsed.channels
    assert RecallChannel.VECTOR in parsed.channels


def test_parser_extracts_time_constraint(world) -> None:
    parsed = world.parser.parse(RetrievalQuery(text="昨天 coffee"))

    assert parsed.time_from is not None
    assert parsed.time_to is not None


def test_simple_parser_uses_sanitized_text_for_raw_and_tokens() -> None:
    parser = SimpleQueryParser(WhitespaceTokenizer(), sanitize=True)

    parsed = parser.parse(
        RetrievalQuery(
            text="[Fri 2026-03-27 06:16 UTC]\n"
            "Sender (untrusted metadata): openJiuwen-bot\n"
            "北京 weather"
        )
    )

    assert parsed.raw == "北京 weather", "raw 应使用清洗后的 query"
    assert parsed.tokens == ["北", "京", "weather"], "tokens 应基于清洗后的 query"


def test_rewrite_disabled_by_default_even_with_llm() -> None:
    """rewrite_enabled 默认 False：即使注入了 LLM，query 也不改写。"""
    llm = _RewriteLLM()
    parser = SimpleQueryParser(WhitespaceTokenizer(), llm=llm)  # rewrite_enabled 默认 False

    parsed = parser.parse(RetrievalQuery(text="hello world"))

    assert parsed.rewritten == "hello world", "rewrite_enabled=False 时不应改写 query"
    assert parsed.tokens == ["hello", "world"]


def test_rewrite_enabled_uses_llm() -> None:
    """rewrite_enabled=True：注入了 LLM 时执行改写。"""
    llm = _RewriteLLM()
    parser = SimpleQueryParser(WhitespaceTokenizer(), llm=llm, rewrite_enabled=True)

    parsed = parser.parse(RetrievalQuery(text="hello world"))

    assert parsed.rewritten == "hello world [rewritten]", "rewrite_enabled=True 时应通过 LLM 改写 query"
    assert "rewritten" in parsed.tokens[-1], "tokens 应基于改写后的 query"


def test_rewrite_disabled_no_llm() -> None:
    """未注入 LLM 时，无论 rewrite_enabled 如何，query 原样透传。"""
    parser = SimpleQueryParser(WhitespaceTokenizer(), rewrite_enabled=True)  # 无 LLM

    parsed = parser.parse(RetrievalQuery(text="hello world"))

    assert parsed.rewritten == "hello world", "无 LLM 时 query 应原样透传"
