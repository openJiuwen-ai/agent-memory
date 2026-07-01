"""Query parser contract tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from common.type_def.filter import FilterClause, FilterOp
from retrieval.query_parser_impl.simple_query_parser import SimpleQueryParser
from retrieval.types import RecallChannel, RetrievalQuery

pytestmark = pytest.mark.unit


def test_parser_transfers_filters_and_as_of(world) -> None:
    as_of = datetime(2026, 6, 10, tzinfo=timezone.utc)
    filters = [FilterClause("tags", FilterOp.CONTAINS, "x")]

    parsed = world.parser.parse(RetrievalQuery(text="hello world", filters=filters, as_of=as_of))

    assert parsed.scalar_filters == filters
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
