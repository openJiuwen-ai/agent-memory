"""Extractor source_id 容错回归测试。

真实故障（R9000K，2026-11-17 05:41）：

    输入 unit id:       ccbf1798
    模型返回 source_id: ccb1798-018c-432c-8f0c-2b73131b0f2f

LLM 抄写短 ID 时丢了一个 ``f``。7 个候选全部校验失败 → candidates 为空
→ InvalidExtractionCandidateError → 整轮 11.5 小时评测终止，无结果落盘。

设计意图：source_id 校验是为了防止「候选绑到错误的 unit」，不是拒绝近似 ID。
在批次内可唯一确定时，单字符抄写错误应当被容错，而不是升级为致命错误。
"""
from __future__ import annotations

import pytest

from jiuwen_memory.construction.extractor_impl.llm_extractor import (
    resolve_source_id,
)


def _ids(*values: str) -> list[str]:
    return list(values)


def test_exact_match_wins() -> None:
    """精确匹配时原样返回。"""
    batch = _ids("ccbf1798", "a1b2c3d4")
    assert resolve_source_id("ccbf1798", batch) == "ccbf1798"


def test_strips_uuid_shell_before_matching() -> None:
    """模型把短 id 补成了完整 UUID —— 取前 8 位后仍应匹配。"""
    batch = _ids("ccbf1798")
    assert resolve_source_id(
        "ccbf1798-018c-432c-8f0c-2b73131b0f2f", batch
    ) == "ccbf1798"


def test_uuid_with_typo_in_prefix_still_resolves() -> None:
    """真实故障：短 id 被补成 UUID 且前缀抄错一个字符。

    输入 ccbf1798 → 模型返回 ccb1798-018c-432c-8f0c-2b73131b0f2f
    """
    batch = _ids("ccbf1798")
    assert resolve_source_id(
        "ccb1798-018c-432c-8f0c-2b73131b0f2f", batch
    ) == "ccbf1798"


def test_uuid_with_typo_is_rejected_when_ambiguous() -> None:
    """两个候选的前缀编辑距离都是 1 —— 无法判定，必须丢弃。"""
    batch = _ids("ccbf1798", "ccbd1798")
    assert resolve_source_id(
        "ccbe1798-018c-432c-8f0c-2b73131b0f2f", batch
    ) is None


def test_single_character_typo_resolves_when_unambiguous() -> None:
    """真实故障场景：ccb1798 抄自 ccbf1798（丢一个字符），批次内唯一 → 容错。"""
    batch = _ids("ccbf1798", "11112222", "33334444")
    assert resolve_source_id("ccb1798", batch) == "ccbf1798"


def test_typo_does_not_resolve_when_ambiguous() -> None:
    """两个候选距离相同 —— 无法判定，必须返回 None（宁可重试也不绑错）。"""
    batch = _ids("ccbf1798", "ccbe1798")
    assert resolve_source_id("ccbd1798", batch) is None


def test_unknown_id_beyond_distance_returns_none() -> None:
    """差异过大时不匹配 —— 防止把完全无关的候选硬绑上去。"""
    batch = _ids("ccbf1798")
    assert resolve_source_id("zzzz9999", batch) is None


def test_empty_source_id_returns_none() -> None:
    assert resolve_source_id("", _ids("ccbf1798")) is None


def test_empty_batch_returns_none() -> None:
    assert resolve_source_id("ccbf1798", []) is None


def test_exact_match_still_wins_over_nearby_id() -> None:
    """精确命中优先于近似匹配，即使批次里有更"近"的。"""
    batch = _ids("abc12345", "abc12346")
    assert resolve_source_id("abc12345", batch) == "abc12345"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("ccbf1798", "ccbf1798"),  # 精确
        ("ccb1798", "ccbf1798"),   # 单字符丢失
        ("ccbf11798", "ccbf1798"), # 单字符插入
        ("ccbf1799", "ccbf1798"),  # 单字符替换
        ("cbf1798", "ccbf1798"),   # 首位丢失
    ],
)
def test_transposition_style_typos_within_distance(given: str, expected: str) -> None:
    """编辑距离 1 以内的抄写错误都应能容错。"""
    assert resolve_source_id(given, _ids("ccbf1798")) == expected


def test_two_character_difference_is_not_tolerated() -> None:
    """距离 2 不放行 —— 容错范围必须收紧，否则匹配会变宽到不可控。"""
    assert resolve_source_id("ccbf189", _ids("ccbf1798")) is None
