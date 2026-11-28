# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Sanitize must not delete user content that merely mentions UTC."""

from jiuwen_memory.retrieval.query_parser_impl.sanitize import sanitize_query


def test_generated_timestamp_wrapper_is_still_stripped() -> None:
    assert sanitize_query("[Fri 2026-03-27 06:16 UTC] weather") == "weather"
    assert sanitize_query("[2026-03-27 06:16 UTC] weather") == "weather"


def test_arbitrary_utc_bracket_is_preserved() -> None:
    assert sanitize_query("[CPU UTC] scheduling") == "[CPU UTC] scheduling"
