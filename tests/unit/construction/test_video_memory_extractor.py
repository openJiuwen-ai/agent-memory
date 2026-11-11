from __future__ import annotations

import json

import pytest

from jiuwen_memory.common.normalizer.normalizer_impl.video_normalizer import (
    VideoNormalizer,
)
from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryUnit,
    Modality,
    RawPayload,
    Scope,
    Segment,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.construction.extractor_impl.video_memory_extractor import (
    VideoMemoryExtractor,
)
from jiuwen_memory.ingest.ingestor_impl.simple_ingestor import SimpleIngestor

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


def _video_backend(payload: RawPayload) -> tuple[list[dict], list[dict]]:
    """4 个 clip + 2 个 event，detailed_caption 撑大包络使其远超默认 8000 字符。"""
    del payload
    clips = [
        {
            "id": f"clip-{i}",
            "time_range": [i * 30, (i + 1) * 30],
            "visual_summary": "画面摘要",
            "detailed_caption": "详细描述" * 1000,
            "ASR": "语音转写",
            "environment": "室内",
        }
        for i in range(4)
    ]
    events = [
        {
            "task_id": "event-1",
            "time_span": [0, 60],
            "child_clip_ids": ["clip-0", "clip-1"],
            "topic": "开场",
            "summary": "前两段",
        },
        {
            "task_id": "event-2",
            "time_span": [60, 120],
            "child_clip_ids": ["clip-2", "clip-3"],
            "topic": "收尾",
            "summary": "后两段",
        },
    ]
    return clips, events


def test_oversized_video_envelope_survives_ingest_and_extracts_all_memories() -> None:
    """issue #294：超过默认 max_unit_chars 的视频包络经 SimpleIngestor 后仍能抽出全部 CLM/ELM。"""
    ingestor = SimpleIngestor(VideoNormalizer(backend=_video_backend))
    payload = RawPayload(
        id="video-1", scope=Scope(user="user-1"), modality=Modality.VIDEO, uri="file:///v.mp4"
    )

    sources = ingestor.ingest([payload])
    assert len(sources) == 1, "视频包络应保持单 unit"
    assert len(sources[0].content) > 8000, "用例前提：包络必须超过默认 max_unit_chars"

    derived = VideoMemoryExtractor().extract(sources)

    assert len(derived) == 6, "4 个 clip + 2 个 event 应全部抽出"
    levels = sorted(str(u.system_metadata.get("memory_level")) for u in derived)
    assert levels == ["clm"] * 4 + ["elm"] * 2, "应产出 4 条 CLM 与 2 条 ELM"


def test_derived_clm_and_elm_write_origin_inferred_and_table_importance() -> None:
    """F10：CLM/ELM 是派生记忆——origin=inferred、重要度按 default 类别查表（覆盖源值）。"""
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
                        "events": [
                            {
                                "id": "event-1",
                                "start_seconds": 0.0,
                                "end_seconds": 1.0,
                                "topic": "e",
                                "clip_ids": ["clip-1"],
                            }
                        ],
                    }
                ),
                source=Modality.VIDEO,
            )
        ],
        source_ref="video-1",
        system_metadata={"origin": "original", "importance": "0.95"},
    )

    units = VideoMemoryExtractor(importance_category_map={"default": "0.35"}).extract([source])

    assert len(units) == 2
    for unit in units:
        assert unit.system_metadata["origin"] == "inferred"
        assert unit.system_metadata["importance"] == "0.35"
