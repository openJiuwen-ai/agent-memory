# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""InMemoryFulltextStore 的 BM25 排序性质。

三条断言各钉住 BM25 相对「词重叠占比」多出的一项，缺任一项排序都会变：IDF、
词频饱和（k1）、按 avgdl 缩放的长度归一（b）。第四条把打分与 Lucene 口径的
参考实现逐条对齐，防止公式在重构中漂移。
"""

import math

import pytest

from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.fulltext_impl.in_memory_fulltext_store import InMemoryFulltextStore
from jiuwen_memory.storage.types import Document, TextQuery

SCOPE = Scope(org="t", user="u")


def _store(docs: dict[str, str]) -> InMemoryFulltextStore:
    store = InMemoryFulltextStore(WhitespaceTokenizer())
    store.insert(SCOPE, [Document(id=k, text=v) for k, v in docs.items()])
    return store


def _ranked(store: InMemoryFulltextStore, text: str) -> list[str]:
    return [hit.id for hit in store.search(SCOPE, TextQuery(text=text, top_k=100))]


def test_rare_term_outranks_common_terms():
    """IDF：含罕见词的短文档 > 只堆砌高频词的长文档。"""
    docs = {f"noise{i}": "the report was sent to the team on the same day"
            for i in range(20)}
    docs["rare"] = "the kallax bookshelf report"
    ranked = _ranked(_store(docs), "the report about the kallax bookshelf")
    assert ranked[0] == "rare"


def test_term_frequency_saturates():
    """k1：重复同一词的收益递减，不随词频线性增长。"""
    store = _store({
        "x1": "coffee " * 1 + "filler word here",
        "x10": "coffee " * 10 + "filler word here",
        "x40": "coffee " * 40 + "filler word here",
    })
    by_id = {hit.id: hit.score for hit in store.search(SCOPE, TextQuery(text="coffee", top_k=10))}
    first = by_id["x10"] - by_id["x1"]
    second = by_id["x40"] - by_id["x10"]
    assert 0 < second < first, "增益应递减（饱和），而非线性累加"


def test_short_document_does_not_win_by_length_alone():
    """b：长度归一按 avgdl 缩放，单词文档不再靠「命中率 100%」登顶。"""
    docs = {"tiny": "coffee"}
    docs["relevant"] = "we talked about coffee and the coffee machine in the kitchen"
    docs.update({f"noise{i}": "an unrelated sentence about other things" for i in range(20)})
    assert _ranked(_store(docs), "coffee machine")[0] == "relevant"


def test_scores_match_reference_formula():
    """与 Lucene 口径 BM25 逐条对齐（log 内 +1，故 IDF 恒非负）。"""
    texts = {"a": "alice likes coffee",
             "b": "bob drinks coffee every single morning without fail",
             "c": "coffee"}
    tokenizer = WhitespaceTokenizer()
    tokenised = {k: tokenizer.tokenize(v) for k, v in texts.items()}
    q_terms = set(tokenizer.tokenize("alice coffee"))
    k1, b = 1.5, 0.75
    n = len(tokenised)
    avgdl = sum(len(t) for t in tokenised.values()) / n

    expected = {}
    for doc_id, tokens in tokenised.items():
        score = 0.0
        for term in q_terms:
            freq = tokens.count(term)
            if not freq:
                continue
            df = sum(1 for t in tokenised.values() if term in t)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            score += idf * (freq * (k1 + 1.0)) / (
                freq + k1 * (1.0 - b + b * len(tokens) / avgdl))
        expected[doc_id] = score

    actual = {hit.id: hit.score
              for hit in _store(texts).search(SCOPE, TextQuery(text="alice coffee", top_k=10))}
    assert actual.keys() == expected.keys()
    for doc_id, score in expected.items():
        assert actual[doc_id] == pytest.approx(score, abs=1e-12)


@pytest.mark.parametrize("kwargs", [{"k1": -1.0}, {"b": -0.1}, {"b": 1.5}])
def test_rejects_out_of_range_parameters(kwargs):
    with pytest.raises(ValueError):
        InMemoryFulltextStore(WhitespaceTokenizer(), **kwargs)
