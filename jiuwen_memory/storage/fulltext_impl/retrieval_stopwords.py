"""全文检索查询侧的中文停用词表。

仅过滤不承载检索意图的高频中文结构虚词。刻意保留“不/没有/什么/如何”等
否定词和疑问词，避免改变查询语义。

各全文后端（Elasticsearch / 未来可能的 OpenSearch、Lucene 等）在
查询分词后统一引用本表做停用词过滤，避免重复维护。
"""

from __future__ import annotations

RETRIEVAL_STOPWORDS: frozenset[str] = frozenset(
    {"的", "了", "在", "是", "这", "也", "和", "就", "都", "而", "及", "与", "着", "或"}
)
