# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""FixedWindowChunker 空输入/基本切分契约。"""

from jiuwen_memory.common.chunker.chunker_impl.fixed_window_chunker import FixedWindowChunker


def test_blank_content_has_no_chunks() -> None:
    chunker = FixedWindowChunker(size=4)
    assert chunker.chunk("") == []
    assert chunker.chunk("   \n\t ") == []


def test_non_blank_window_offsets_and_token_counts_unchanged() -> None:
    chunker = FixedWindowChunker(size=4)
    chunks = chunker.chunk("abcdefghij", "u1")
    assert [c.text for c in chunks] == ["abcd", "efgh", "ij"]
    assert [(c.start, c.end) for c in chunks] == [(0, 4), (4, 8), (8, 10)]
    assert [c.token_count for c in chunks] == [4, 4, 2]
    assert all(c.unit_id == "u1" for c in chunks)
