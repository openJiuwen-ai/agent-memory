# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""调用级动态 prompt 的解析与传递工具。

metadata 只写 prompt 的 **key**（引用 yml ``prompts`` 段里的命名 prompt），真实文本
由 :class:`~construction.prompt_registry.PromptRegistry` 在运行时按 key 查询。本模块
负责从 metadata 解析出 ``(strategy, prompt_key)`` 列表，并把 consolidation/reflect
prompt key 从源 unit 透传给派生候选。
"""

from __future__ import annotations

from collections.abc import Mapping

from jiuwen_memory.common.type_def import MemoryUnit, MetadataValueType

EXTRACT_PROMPT_PREFIX = "_extract_prompt_"
CONSOLIDATION_PROMPT_PREFIX = "_consolidation_prompt_"
REFLECT_PROMPT_PREFIX = "_reflect_prompt_"
EXTRACTION_STRATEGY_KEY = "_extraction_strategy"


def parse_prompt_strategies(
    metadata: Mapping[str, MetadataValueType],
    prefix: str,
) -> list[tuple[str, str]]:
    """按 metadata 插入顺序解析 ``prefix + strategy`` 键，返回 ``(strategy, prompt_key)``。

    ``prompt_key`` 是引用 yml ``prompts`` 段的命名 key，不是 prompt 文本本身。
    """
    result: list[tuple[str, str]] = []
    for key, value in metadata.items():
        if not key.startswith(prefix):
            continue
        strategy = key[len(prefix):].strip()
        prompt_key = str(value).strip()
        if strategy and prompt_key:
            result.append((strategy, prompt_key))
    return result


def prompts_from_units(
    units: list[MemoryUnit],
    prefix: str,
) -> list[tuple[str, str]]:
    """合并一批单元的 prompt key，按首次出现顺序去重。"""
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for unit in units:
        for strategy, prompt_key in parse_prompt_strategies(unit.system_metadata, prefix):
            if strategy in seen:
                continue
            seen.add(strategy)
            result.append((strategy, prompt_key))
    return result


def copy_consolidation_prompts(
    sources: list[MemoryUnit],
    targets: list[MemoryUnit],
) -> None:
    """把调用级 consolidation prompt key 传给派生候选，供落盘前消费。"""
    prompts = prompts_from_units(sources, CONSOLIDATION_PROMPT_PREFIX)
    if not prompts:
        return
    values = {
        f"{CONSOLIDATION_PROMPT_PREFIX}{strategy}": prompt_key
        for strategy, prompt_key in prompts
    }
    for target in targets:
        target.system_metadata.update(values)


def copy_reflect_prompts(
    sources: list[MemoryUnit],
    targets: list[MemoryUnit],
) -> None:
    """把调用级 reflect prompt key 传给派生候选，供反思步消费。"""
    prompts = prompts_from_units(sources, REFLECT_PROMPT_PREFIX)
    if not prompts:
        return
    values = {
        f"{REFLECT_PROMPT_PREFIX}{strategy}": prompt_key
        for strategy, prompt_key in prompts
    }
    for target in targets:
        target.system_metadata.update(values)
