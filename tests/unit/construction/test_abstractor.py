# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""LLMAbstractor 内容型分批单测（检视 P2：8000 截断静默漏事实 / 跨记忆归纳）。"""

import json

import pytest

from jiuwen_memory.construction.abstractor_impl.llm_abstractor import LLMAbstractor
from tests.unit.construction.fixtures import (
    RecordingLLM,
    RuleFeatureExtractor,
    create_test_unit,
)

pytestmark = pytest.mark.unit


def _make_abstractor(llm):
    return LLMAbstractor(
        llm=llm,
        feature_extractor=RuleFeatureExtractor(),
        min_confidence=0.5,
        max_attempts=3,
        retry_backoff_ms=0,
    )


def test_abstract_oversized_unit_fully_consumed_in_chunks():
    """>8000 unit 两段式消费：先逐片提炼覆盖全部原文，再对提炼视图归纳。"""
    middle_fact = "中段事实丙在此。"
    content = "X" * 8000 + middle_fact + "Y" * 7984  # 16000 chars
    llm = RecordingLLM([
        json.dumps([{"content": "头部抽象", "evidence": "头部", "confidence": 0.9}]),
        json.dumps([{"content": "中段抽象", "evidence": middle_fact, "confidence": 0.9}]),
        json.dumps([{"content": "整体摘要", "evidence": "提炼视图", "confidence": 0.9}]),
    ])
    abst = _make_abstractor(llm)
    result = abst.abstract([create_test_unit("u1", content)])

    assert len(llm.user_texts) == 3  # 提炼×2 + 归纳×1
    assert all(len(t) <= 8300 for t in llm.user_texts[:2])  # 提炼单批 prompt 有界
    assert middle_fact not in llm.user_texts[0]
    assert middle_fact in llm.user_texts[1]
    # 归纳调用看到的是提炼视图，不再重复投喂原始正文
    assert "头部抽象" in llm.user_texts[2] and "中段抽象" in llm.user_texts[2]
    assert "X" * 8000 not in llm.user_texts[2]
    contents = [u.content for u in result]
    assert "头部抽象" in contents and "中段抽象" in contents and "整体摘要" in contents


def test_abstract_mixed_group_induces_across_units():
    """PATTERN 组：短记忆与长记忆同批归纳——分片不拆散跨记忆共同归纳。"""
    s1 = create_test_unit("s1", "短记忆甲：周五都点湘菜。")
    s2 = create_test_unit("s2", "短记忆乙：周三都点粤菜。")
    l1 = create_test_unit("l1", "L" * 8000 + "重复模式丙出现在此。" + "M" * 7990)
    llm = RecordingLLM([
        json.dumps([{"content": "长片提炼甲", "evidence": "头部", "confidence": 0.9}]),
        json.dumps([{"content": "长片提炼乙", "evidence": "中段", "confidence": 0.9}]),
        json.dumps([{"content": "共同模式X", "evidence": "甲乙丙", "confidence": 0.9, "pattern_name": "weekly"}]),
    ])
    abst = _make_abstractor(llm)
    result = abst.abstract([s1, s2, l1])

    assert len(llm.user_texts) == 3  # 提炼×2（仅长记忆分片）+ 归纳×1
    final_text = llm.user_texts[2]
    assert "短记忆甲" in final_text and "短记忆乙" in final_text  # 短记忆原文参与归纳
    assert "长片提炼甲" in final_text and "长片提炼乙" in final_text  # 长记忆以提炼视图同批参与
    contents = [u.content for u in result]
    assert "共同模式X" in contents  # 跨记忆归纳产出
