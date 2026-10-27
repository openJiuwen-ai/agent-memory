# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""LLMAbstractor 内容型分批单测（检视 P2：抽象 8000 截断静默漏事实）。"""

import json

import pytest

from jiuwen_memory.construction.abstractor_impl.llm_abstractor import LLMAbstractor
from tests.unit.construction.fixtures import RecordingLLM, RuleFeatureExtractor, create_test_unit

pytestmark = pytest.mark.unit


def test_abstract_oversized_unit_fully_consumed_in_chunks():
    """>8000 unit 分批完整消费：逐批调用、候选跨批合并，中段事实不丢。"""
    middle_fact = "中段事实丙在此。"
    content = "X" * 8000 + middle_fact + "Y" * 7984  # 16000 chars
    llm = RecordingLLM([
        json.dumps([{"content": "头部抽象", "evidence": "头部", "confidence": 0.9}]),
        json.dumps([{"content": "中段抽象", "evidence": middle_fact, "confidence": 0.9}]),
    ])
    abst = LLMAbstractor(
        llm=llm,
        feature_extractor=RuleFeatureExtractor(),
        min_confidence=0.5,
        max_attempts=3,
        retry_backoff_ms=0,
    )
    unit = create_test_unit("u1", content)
    result = abst.abstract([unit])

    assert len(llm.user_texts) == 2  # 分两批调用
    assert all(len(t) <= 8300 for t in llm.user_texts)  # 单批 prompt 有界
    assert middle_fact not in llm.user_texts[0]
    assert middle_fact in llm.user_texts[1]
    assert len(result) == 2  # 两批候选都进入构建
