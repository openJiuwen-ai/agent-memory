from __future__ import annotations

import json

import pytest

from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.construction.extractor_impl.video_memory_extractor import (
    VideoMemoryExtractor,
)

pytestmark = pytest.mark.unit


def test_video_memory_times_are_float_metadata() -> None:
    source = MemoryUnit(
        id="source-1",
        scope=Scope(user="user-1"),
        segments=[
            Segment(
                content=json.dumps(
                    {
                        "payload_id": "video-1",
                        "clips": [
                            {
                                "id": "clip-1",
                                "start_seconds": 1.9,
                                "end_seconds": 30.8,
                                "visual_summary": "A person enters the room.",
                            }
                        ],
                        "events": [
                            {
                                "id": "event-1",
                                "start_seconds": 1.9,
                                "end_seconds": 30.8,
                                "topic": "Room entry",
                                "clip_ids": ["clip-1"],
                            }
                        ],
                    }
                ),
                source=Modality.VIDEO,
            )
        ],
        source_ref="video-1",
    )

    units = VideoMemoryExtractor().extract([source])

    assert len(units) == 2
    assert all(unit.system_metadata["start_seconds"] == 1.9 for unit in units)
    assert all(unit.system_metadata["end_seconds"] == 30.8 for unit in units)
    assert all(isinstance(unit.system_metadata["start_seconds"], float) for unit in units)
    assert all(isinstance(unit.system_metadata["end_seconds"], float) for unit in units)

    restored = loads(dumps(units[0]))
    assert restored is not None
    assert isinstance(restored.system_metadata["start_seconds"], float)
    assert isinstance(restored.system_metadata["end_seconds"], float)


def test_elm_child_clm_source_ids_is_list_after_codec() -> None:
    """#1: child_clm_source_ids 直存 list[str]，codec 往返仍是 list，CONTAINS 可命中子 clip id。"""
    source = MemoryUnit(
        id="source-1",
        scope=Scope(user="user-1"),
        segments=[
            Segment(
                content=json.dumps(
                    {
                        "payload_id": "video-1",
                        "clips": [
                            {
                                "id": "clip-1",
                                "start_seconds": 0.0,
                                "end_seconds": 10.0,
                                "visual_summary": "A presenter opens a slide.",
                            }
                        ],
                        "events": [
                            {
                                "id": "event-1",
                                "start_seconds": 0.0,
                                "end_seconds": 10.0,
                                "topic": "Opening",
                                "clip_ids": ["clip-1"],
                            }
                        ],
                    }
                ),
                source=Modality.VIDEO,
            )
        ],
        source_ref="video-1",
    )

    units = VideoMemoryExtractor().extract([source])
    elm = next(u for u in units if u.system_metadata["memory_level"] == "elm")

    child_ids = elm.system_metadata["child_clm_source_ids"]
    assert isinstance(child_ids, list), (
        "child_clm_source_ids 应为 list[str]，不应是 json.dumps 后的字符串"
    )
    assert child_ids == ["clip-1"]
    assert all(isinstance(x, str) for x in child_ids)

    restored = loads(dumps(elm))
    assert restored is not None
    restored_ids = restored.system_metadata["child_clm_source_ids"]
    assert isinstance(restored_ids, list), "codec 往返后 child_clm_source_ids 仍应为 list"
    assert restored_ids == ["clip-1"]

    # CONTAINS 语义：fusion store 的 CONTAINS 评估在 list 上是 `clause.value in val`，
    # 这里直接断言该原语命中子 clip id、不命中不存在的 id。
    assert "clip-1" in restored_ids
    assert "clip-missing" not in restored_ids


def test_extract_skips_derived_clm_unit() -> None:
    """#5: 派生 CLM unit（provenance 非空、content 非 JSON）调 extract 返空，不抛。"""
    clm_unit = MemoryUnit(
        id="clm-1",
        scope=Scope(user="user-1"),
        segments=[
            Segment(content="Visual summary: not a JSON object", source=Modality.VIDEO)
        ],
        source_ref="video-1",
        provenance=["source-1"],
    )

    result = VideoMemoryExtractor().extract([clm_unit])

    assert result == []


def test_extract_rejects_non_source_video_units_before_parsing() -> None:
    scope = Scope(user="user-1")
    units = [
        MemoryUnit(
            id="text-1",
            scope=scope,
            segments=[
                Segment(
                    content=json.dumps({"clips": [], "events": []}),
                    source=Modality.TEXT,
                )
            ],
        ),
        MemoryUnit(
            id="derived-1",
            scope=scope,
            segments=[Segment(content="not JSON", source=Modality.VIDEO)],
            system_metadata={"modal_type": "multimodal"},
        ),
        MemoryUnit(
            id="archived-1",
            scope=scope,
            segments=[Segment(content="not JSON", source=Modality.VIDEO)],
            lifecycle=LifecycleState.ARCHIVED,
        ),
    ]

    assert VideoMemoryExtractor().extract(units) == []


def test_derived_metadata_strips_call_level_switches() -> None:
    """派生记忆剥离调用开关，并保留用户元数据。"""
    source = MemoryUnit(
        id="source-1",
        scope=Scope(user="user-1"),
        segments=[
            Segment(
                content=json.dumps(
                    {
                        "payload_id": "video-1",
                        "clips": [
                            {
                                "id": "clip-1",
                                "start_seconds": 0.0,
                                "end_seconds": 1.0,
                                "visual_summary": "v",
                            }
                        ],
                        "events": [],
                    }
                ),
                source=Modality.VIDEO,
            )
        ],
        source_ref="video-1",
        system_metadata={"infer": "true", "pipeline": "video"},
        user_metadata={"custom_key": "keep"},
    )

    units = VideoMemoryExtractor().extract([source])

    assert len(units) == 1
    clm = units[0]
    assert "infer" not in clm.system_metadata
    assert "pipeline" not in clm.system_metadata
    assert clm.user_metadata.get("custom_key") == "keep"
