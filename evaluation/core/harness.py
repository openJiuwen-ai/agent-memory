"""EvalHarness——用注入的 MemoryAPI 把数据集灌入并跑查询，产出可打分的原始观测。

走 ``MemoryAPI`` 公共面（``write`` / ``recall``），因此评测的是「整体功能」而非
检索层孤件：``write`` 经接入/构建/索引落库，``recall`` 经查询理解/多路召回/融合/
披露返回。``key→unit_id`` 映射在写入时捕获（``write`` 返回本次创建的 ``MemoryUnit``），
使数据集的逻辑相关集能映射到真实 ``unit_id`` 再与召回结果比对。

当前仓库提供 evaluation-only baseline adapter。真实 MemoryAPI 装配接入后，调用方
可以继续通过 ``api`` 或 ``api_factory(config)`` 把装配产物注入进来。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .types import CaseOutcome, Dataset, MemorySeed, QueryCase

ApiFactory = Callable[[Optional[Any]], Any]

_MISSING_ADAPTER = (
    "EvalHarness requires a MemoryAPI instance or api_factory; "
    "use evaluation.api_adapter.build_evaluation_api for the local baseline, "
    "or wire the real MemoryAPI adapter."
)


class EvalHarness:
    """MemoryAPI 注入 → 灌语料 → 跑查询 → 采集观测。"""

    def __init__(
        self,
        api: Optional[Any] = None,
        api_factory: Optional[ApiFactory] = None,
        config: Optional[Any] = None,
    ) -> None:
        if api is None and api_factory is not None:
            api = api_factory(config)
        if api is None:
            raise RuntimeError(_MISSING_ADAPTER)
        self._api = getattr(api, "api", api)
        self._key2ids: Dict[str, List[str]] = {}

    def ingest(self, seeds: List[MemorySeed]) -> None:
        """逐条写入语料，捕获每个数据集 key 对应的真实 unit_id（可为多条：规约/切分）。"""
        for seed in seeds:
            units = self._api.write(
                seed.content,
                seed.scope,
                actor=seed.scope,
                tags=list(seed.tags),
                metadata=dict(seed.metadata),
                occurred_at=seed.occurred_at,
            )
            self._key2ids[seed.key] = [u.id for u in units]

    def run_query(self, case: QueryCase) -> CaseOutcome:
        """执行一次 recall，把 ground truth key 映射为物理 id，连同轨迹打包为观测。"""
        result = self._api.recall(
            case.text,
            case.scope,
            actor=case.scope,
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
