# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""合并抽取 A/B 对比（F10 决策 11）。

对同一批输入分别以 ``merge_extract=True/False`` 写入，输出每次写入的条数、派生条目
数、引擎调用次数与耗时。用途是回答两个问题：

1. 合并抽取是否值得默认开启——看派生条目数差异与耗时；
2. 关闭合并的回退路径是否仍然可用——看失败数与调用次数。

脚本不内置 LLM，也不修改内核：调用方传入已装配的引擎（或其替身），导出指标为 JSON。
用法::

    python -m evaluation.scripts.compare_grouped_extract --config path/to/memory.yml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Any

from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.types import BatchWriteItem

logger = logging.getLogger("compare_grouped_extract")

# 评测脚本需要读取引擎内部组件做 A/B 统计，按仓库既有做法整文件豁免。
# pylint: disable=protected-access


def _items(batch: list[str], scope: Scope) -> list[BatchWriteItem]:
    return [
        BatchWriteItem(
            content=content,
            scope=scope,
            system_metadata={"infer": "true"},
        )
        for content in batch
    ]


class _EvolverProbe:
    """包住 Evolver 的抽取入口，统计真实调用次数与输入 token 估算。

    token 用「字符数 / 4」近似：脚本不加载分词器，数值只用于比较两种模式的相对成本。
    """

    def __init__(self, evolver) -> None:
        self._evolver = evolver
        self.extract_calls = 0
        self.input_tokens = 0

    @staticmethod
    def _cost(units) -> int:
        return sum(
            max(1, len(unit.segments[0].content if unit.segments else "") // 4) for unit in units
        )

    def supports_grouped_extract(self) -> bool:
        return self._evolver.supports_grouped_extract()

    def evolve_grouped(self, units):
        self.extract_calls += 1
        self.input_tokens += self._cost(units)
        return self._evolver.evolve_grouped(units)

    def evolve(self, units, mode=None):
        self.extract_calls += 1
        self.input_tokens += self._cost(units)
        return self._evolver.evolve(units, mode)

    def __getattr__(self, name):
        return getattr(self._evolver, name)


class _CountingEngine:
    """记录实际 ``write_group`` 调用次数的代理。

    不能按 ``merge_extract`` 推算调用次数：两种模式下每个 batch 都只调一次
    ``write_group``，差别在引擎内部怎么执行。
    """

    def __init__(self, engine) -> None:
        self.engine = engine
        self.calls = 0

    async def write_group(self, items, *, merge_extract: bool):
        self.calls += 1
        return await self.engine.write_group(items, merge_extract=merge_extract)


async def _run(engine, samples: list[list[str]], *, merge_extract: bool, scope: Scope) -> dict:
    proxy = _CountingEngine(engine)
    probe = None
    evolver = getattr(engine, "_evolver", None)
    if evolver is not None:
        probe = _EvolverProbe(evolver)
        engine._evolver = probe
    started = time.perf_counter()
    derived = 0
    failures = 0
    for batch in samples:
        outcomes = await proxy.write_group(
            _items(batch, scope), merge_extract=merge_extract
        )
        for outcome in outcomes:
            derived += len(outcome.units)
            if outcome.error:
                failures += 1
    return {
        "merge_extract": merge_extract,
        "batches": len(samples),
        "items": sum(len(batch) for batch in samples),
        "write_group_calls": proxy.calls,
        "extract_calls": probe.extract_calls if probe is not None else 0,
        "extract_input_tokens": probe.input_tokens if probe is not None else 0,
        "derived": derived,
        "failures": failures,
        "seconds": round(time.perf_counter() - started, 4),
    }


def compare(engine_factory, samples: list[list[str]]) -> dict[str, Any]:
    """同步入口：两轮各自新建引擎与 scope，返回 ``{"off": ..., "on": ...}``。

    每轮使用独立引擎与独立 org：否则第二轮会看到第一轮写入的记忆，派生数量与
    重复率都不再可比。``engine_factory`` 是零参可调用对象。
    """
    off = asyncio.run(
        _run(engine_factory(), samples, merge_extract=False, scope=Scope(org="eval-off"))
    )
    on = asyncio.run(
        _run(engine_factory(), samples, merge_extract=True, scope=Scope(org="eval-on"))
    )
    return {
        "off": off,
        "on": on,
        "derived_delta": on["derived"] - off["derived"],
        "failure_delta": on["failures"] - off["failures"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合并抽取 A/B 对比")
    parser.add_argument("--config", default=None, help="装配用的 memory 配置（YAML/JSON）")
    parser.add_argument("--samples", default="", help="JSONL：每行一个字符串数组，即一批输入")
    args = parser.parse_args(argv)

    from jiuwen_memory.api import assemble

    config = None
    if args.config:
        from jiuwen_memory.config.config import Config

        config = Config.from_file(args.config)
    api = assemble(config=config)
    try:
        samples = _load_samples(args.samples)
        children = []

        def fresh_engine():
            # 每轮独立装配；子 API 必须在比较结束后关闭，否则执行器线程池与影子索引
            # 会一直留到进程退出。
            child = assemble(config=config)
            children.append(child)
            return child._engine

        try:
            report = compare(fresh_engine, samples)
        finally:
            for child in children:
                child.close()
        _emit(report)
    finally:
        api.close()
    return 0


def _emit(report: dict) -> None:
    """CLI 的机器可读输出走 logging（G.LOG.02），格式器只输出消息本身。"""
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.info("%s", json.dumps(report, ensure_ascii=False, indent=2))


def _load_samples(path: str) -> list[list[str]]:
    if not path:
        return [["部署在集群 A", "昨天复测通过"], ["用户偏好中文回复"]]
    samples: list[list[str]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    return samples


if __name__ == "__main__":
    # 普通脚本入口：出错时由未捕获异常给出非零退出码，不显式 SystemExit。
    main()
