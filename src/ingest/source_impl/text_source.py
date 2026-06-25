"""最小实现：:class:`~ingest.source.Source`——内存文本信息源连接器。

用一批预置文本模拟一类信息源（对话/导入），``fetch`` 把它们拉成统一的
``RawPayload``（TEXT 模态，``occurred_at`` 标在 metadata/字段上）。连接器只「取原始
数据」，不做规约（归 Normalizer）、不做编排（归 Ingestor）。``since`` 非空时只返回
该时间点之后的条目（增量拉取）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from common.type_def import Modality, RawPayload, Scope
from ingest.base import IngestOperatorType
from ingest.source import Source


class TextSource(Source):
    """以预置 ``(text, occurred_at)`` 列表模拟的文本信息源。"""

    def __init__(self, scope: Scope, items: Sequence[Tuple[str, datetime]]) -> None:
        self._scope = scope
        self._items = list(items)

    def operator_type(self) -> IngestOperatorType:
        return IngestOperatorType.SOURCE

    def health(self) -> None:
        return None

    def modalities(self) -> List[Modality]:
        return [Modality.TEXT]

    def fetch(self, since: Optional[datetime] = None) -> List[RawPayload]:
        out: List[RawPayload] = []
        for text, occurred_at in self._items:
            if since is not None and occurred_at <= since:
                continue  # 增量：只取 since 之后
            out.append(
                RawPayload(
                    id=str(uuid.uuid4()),
                    scope=self._scope,
                    modality=Modality.TEXT,
                    data=text.encode("utf-8"),
                    occurred_at=occurred_at,
                )
            )
        return out
