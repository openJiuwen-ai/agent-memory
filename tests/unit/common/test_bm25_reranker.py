# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""BM25Reranker 的排序性质与输出契约。

除 BM25 本身的排序性质外，还钉住两条被下游依赖的契约：输出与输入等长同序
（PipelineRetriever 按下标回填 score），以及取值落在 [0, 1]（apply_threshold
把精排分当作已校准、按绝对刻度比 min_score）。
"""

import pytest

from jiuwen_memory.common.reranker.reranker_impl.bm25_reranker import BM25Reranker
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer


@pytest.fixture
def reranker() -> BM25Reranker:
    return BM25Reranker(WhitespaceTokenizer())


def test_discriminating_term_outranks_batch_wide_term(reranker):
    """批内 IDF：人人都有的词不携带排序信息，批内罕见词才有。"""
    texts = ["coffee with alice",         # 含批内罕见词 alice
             "coffee alone",
             "coffee and more coffee",
             "just coffee"]
    scores = reranker.rerank("alice coffee", texts)
    assert scores.index(max(scores)) == 0


def test_output_is_aligned_and_normalised(reranker):
    """与输入等长同序，且落在 [0, 1]——两者都是下游的硬依赖。"""
    texts = ["alice likes coffee", "bob drinks tea", "coffee coffee coffee"]
    scores = reranker.rerank("alice coffee", texts)
    assert len(scores) == len(texts)
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert max(scores) == pytest.approx(1.0), "按批内最大值归一，最高分应恰为 1.0"


@pytest.mark.parametrize("query,texts", [
    ("", ["alice likes coffee"]),          # 空 query
    ("alice", []),                          # 空候选
    ("zebra", ["alice likes coffee"]),      # 无命中
    ("alice", ["", ""]),                    # 全空文本
])
def test_degenerate_inputs_return_zeros(reranker, query, texts):
    scores = reranker.rerank(query, texts)
    assert len(scores) == len(texts)
    assert all(s == 0.0 for s in scores)


@pytest.mark.parametrize("kwargs", [{"k1": -1.0}, {"b": -0.1}, {"b": 1.5}])
def test_rejects_out_of_range_parameters(kwargs):
    with pytest.raises(ValueError):
        BM25Reranker(WhitespaceTokenizer(), **kwargs)
