# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""视频记忆摘要模型：to_dict/from_dict 必须保留 inferred_intent。"""

from uuid import uuid4

from jiuwen_memory.common.normalizer.normalizer_impl.video_models import ShortTermMemory


def _stm(intent: str = "") -> ShortTermMemory:
    return ShortTermMemory(
        id=uuid4(),
        video_source_path="/tmp/clip.mp4",
        time_range=(0.0, 30.0),
        visual_summary="summary",
        detailed_caption="caption",
        embedding=[0.1, 0.2],
        asr="asr",
        environment="office",
        inferred_intent=intent,
    )


def test_short_term_memory_roundtrip_preserves_inferred_intent() -> None:
    original = _stm("user asks about launch date")
    restored = ShortTermMemory.from_dict(original.to_dict())
    assert restored.inferred_intent == "user asks about launch date"
    assert restored.to_dict() == original.to_dict()


def test_from_dict_reads_inferred_intent() -> None:
    payload = _stm("legacy").to_dict()
    payload["inferred_intent"] = "intent from stream"
    assert ShortTermMemory.from_dict(payload).inferred_intent == "intent from stream"
