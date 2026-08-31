# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""EvalHarness——用真实装配把数据集灌入并跑查询，产出可打分的原始观测。

走 ``MemoryAPI`` 公共面（``add`` / ``search``），因此评测的是「整体功能」而非
检索层孤件：``add`` 经接入/构建/索引落库，``search`` 经查询理解/多路召回/融合/
披露返回。``key→unit_id`` 映射在写入时捕获（``add`` 返回本次创建的 ``MemoryUnit``），
使数据集的逻辑相关集能映射到真实 ``unit_id`` 再与召回结果比对。

每个 harness 持有一套独立的内核（``build_kernel``），天然隔离——不同 Config 的对比
跑分各起一套，互不污染。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.common.security.legacy import legacy_request_context
from jiuwen_memory.common.type_def import Context
from jiuwen_memory.config.config import Config

from .types import CaseOutcome, Dataset, MemorySeed, QueryCase


class EvalHarness:
    """装配内核 → 灌语料 → 跑查询 → 采集观测。"""

    def __init__(self, config: Optional[Config] = None) -> None:
        self._kernel = build_kernel(config=config)
        self._api = self._kernel.api
        self._key2ids: Dict[str, List[str]] = {}

    def ingest(self, seeds: List[MemorySeed]) -> None:
        """逐条写入语料，捕获每个数据集 key 对应的真实 unit_id（可为多条：规约/切分）。"""
        for seed in seeds:
            units = self._api.add(
                seed.content,
                seed.scope,
                security=legacy_request_context(seed.scope),
                tags=list(seed.tags),
                metadata=dict(seed.metadata),
                occurred_at=seed.occurred_at,
            )
            self._key2ids[seed.key] = [u.id for u in units]

    def run_query(self, case: QueryCase) -> CaseOutcome:
        """执行一次 search，把相关性标注 key 映射为物理 id，连同轨迹打包为观测。"""
        result = self._api.search(
            case.text,
            Context(case.scope),
            security=legacy_request_context(case.scope),
            filters=list(case.filters) or None,
            as_of=case.as_of,
            top_k=case.top_k,
            with_trajectory=True,
        )
        relevant_ids = set()
        for key in case.relevant_keys:
            relevant_ids.update(self._key2ids.get(key, []))
        return CaseOutcome(
            query_id=case.query_id,
            query_text=case.text,
            ranked_unit_ids=[item.unit_id for item in result.items],
            relevant_unit_ids=relevant_ids,
            contents=[item.content for item in result.items],
            trajectory=list(result.trajectory),
            expected_answer=case.expected_answer,
            metadata=dict(case.metadata),
        )

    def evaluate(self, dataset: Dataset) -> List[CaseOutcome]:
        """灌入数据集全部语料后，逐 query 采集观测。"""
        self.ingest(list(dataset.seeds()))
        return [self.run_query(case) for case in dataset.queries()]
