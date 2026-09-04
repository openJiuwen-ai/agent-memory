"""PipelineRetriever 文档模式的 DOCUMENT 通道注入（F08 §5.6 S14 根因之二）。

文档模式 ShadowRecaller.channel()=DOCUMENT，但 SimpleQueryParser 产出的
``parsed.channels`` 默认 [KEYWORD, GRAPH]（+可选 VECTOR）不含 DOCUMENT；storage.recall
按 ``recaller.channel() in channels`` 过滤 recaller，DOCUMENT 不在 enabled 里则
ShadowRecaller 被静默过滤 → 召回落空。retrieve() 据此在 doc_mode 下把 DOCUMENT 补进
enabled（仅补不替；调用方显式 query.channels 仍尊重其选择）。失效方向：漏补即文档模式
召回恒空且不报错。

本文件只测 ``retrieve()`` 里那段通道补全分支，用假 storage/parser/fuser/discloser
记录 storage.recall 收到的 channels，不依赖真实召回后端。
"""

from __future__ import annotations

import pytest

from jiuwen_memory.common.type_def import (
    ParsedQuery,
    RecallResult,
    RetrievalPipeline,
    Scope,
)
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.types import RecallChannel, RetrievalQuery

pytestmark = pytest.mark.unit


class _RecordingStorage:
    """记录 recall 收到的 channels，其余召回/点读全返空。"""

    def __init__(self) -> None:
        self.recall_channels: list[RecallChannel] | None = None

    def preferred_retrieval_pipeline(self) -> RetrievalPipeline:
        return RetrievalPipeline.RECALL_GET_RANK

    def recall(self, scope, query, *, channels, recall_limit):
        self.recall_channels = channels
        return RecallResult(batches=[])

    def get(self, scope, unit_ids):
        return []


class _ScriptedParser:
    def __init__(self, channels: list[RecallChannel]) -> None:
        self._channels = channels

    def parse(self, query) -> ParsedQuery:
        return ParsedQuery(raw="hello world", channels=list(self._channels))


class _EmptyFuser:
    def fuse(self, query, candidates):
        return []


class _EmptyDiscloser:
    def disclose(self, query, candidates, units, level, max_tokens=None):
        return []


def _retriever(storage: _RecordingStorage, channels, *, doc_mode: bool) -> PipelineRetriever:
    return PipelineRetriever(
        parser=_ScriptedParser(channels),
        fuser=_EmptyFuser(),
        discloser=_EmptyDiscloser(),
        unit_reader=None,
        storage=storage,
        doc_mode=doc_mode,
    )


def test_doc_mode_injects_document_channel() -> None:
    """doc_mode 下 parser 建议的 [KEYWORD, GRAPH] 须补进 DOCUMENT，否则召回落空。"""
    storage = _RecordingStorage()
    retriever = _retriever(
        storage, [RecallChannel.KEYWORD, RecallChannel.GRAPH], doc_mode=True
    )

    retriever.retrieve(Scope(), RetrievalQuery(text="hello world"))

    assert storage.recall_channels == [
        RecallChannel.KEYWORD,
        RecallChannel.GRAPH,
        RecallChannel.DOCUMENT,
    ]


def test_non_doc_mode_does_not_inject_document_channel() -> None:
    storage = _RecordingStorage()
    retriever = _retriever(
        storage, [RecallChannel.KEYWORD, RecallChannel.GRAPH], doc_mode=False
    )

    retriever.retrieve(Scope(), RetrievalQuery(text="hello world"))

    assert storage.recall_channels == [RecallChannel.KEYWORD, RecallChannel.GRAPH]


def test_doc_mode_still_respects_explicit_query_channels() -> None:
    """调用方显式传 query.channels 时仅补 DOCUMENT，不替换其选择。"""
    storage = _RecordingStorage()
    retriever = _retriever(storage, [RecallChannel.KEYWORD], doc_mode=True)

    retriever.retrieve(Scope(), RetrievalQuery(text="hello", channels=[RecallChannel.KEYWORD]))

    assert storage.recall_channels == [RecallChannel.KEYWORD, RecallChannel.DOCUMENT]


def test_doc_mode_does_not_duplicate_existing_document_channel() -> None:
    storage = _RecordingStorage()
    retriever = _retriever(storage, [RecallChannel.KEYWORD], doc_mode=True)

    retriever.retrieve(
        Scope(), RetrievalQuery(text="hello", channels=[RecallChannel.DOCUMENT])
    )

    assert storage.recall_channels == [RecallChannel.DOCUMENT]
