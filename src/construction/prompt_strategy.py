"""调用级动态 prompt 的解析与传递工具。"""

from __future__ import annotations

from collections.abc import Mapping

from common.type_def import MemoryUnit

EXTRACT_PROMPT_PREFIX = "_extract_prompt_"
CONSOLIDATION_PROMPT_PREFIX = "_consolidation_prompt_"
EXTRACTION_STRATEGY_KEY = "_extraction_strategy"


def parse_prompt_strategies(
    metadata: Mapping[str, str],
    prefix: str,
) -> list[tuple[str, str]]:
    """按 metadata 插入顺序解析 ``prefix + strategy`` prompt。"""
    result: list[tuple[str, str]] = []
    for key, value in metadata.items():
        if not key.startswith(prefix):
            continue
        strategy = key[len(prefix):].strip()
        prompt = str(value).strip()
        if strategy and prompt:
            result.append((strategy, prompt))
    return result


def prompts_from_units(
    units: list[MemoryUnit],
    prefix: str,
) -> list[tuple[str, str]]:
    """合并一批单元的 prompt，按首次出现顺序去重。"""
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for unit in units:
        for strategy, prompt in parse_prompt_strategies(unit.metadata, prefix):
            if strategy in seen:
                continue
            seen.add(strategy)
            result.append((strategy, prompt))
    return result


def copy_consolidation_prompts(
    sources: list[MemoryUnit],
    targets: list[MemoryUnit],
) -> None:
    """把调用级 consolidation prompt 传给派生候选，供落盘前消费。"""
    prompts = prompts_from_units(sources, CONSOLIDATION_PROMPT_PREFIX)
    if not prompts:
        return
    values = {f"{CONSOLIDATION_PROMPT_PREFIX}{strategy}": prompt for strategy, prompt in prompts}
    for target in targets:
        target.metadata.update(values)
