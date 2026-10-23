# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""视频规约输出必须保留后端给出的 inferred_intent。"""

import json

from jiuwen_memory.common.normalizer.normalizer_impl.video_normalizer import VideoNormalizer
from jiuwen_memory.common.type_def import Modality, RawPayload

CLIP = {
    "id": "clip-1",
    "time_range": [0.0, 30.0],
    "visual_summary": "summary",
    "detailed_caption": "caption",
    "ASR": "asr",
    "environment": "office",
    "inferred_intent": "user asks about launch date",
}


def test_normalized_clip_keeps_inferred_intent() -> None:
    normalizer = VideoNormalizer(backend=lambda payload: ([CLIP], []))

    content = normalizer.normalize(RawPayload(id="p1", modality=Modality.VIDEO, uri="file:///clip.mp4"))
    clip = json.loads(content)["clips"][0]

    assert clip["inferred_intent"] == "user asks about launch date"
