"""Construction extractor for hierarchical video memories."""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from typing import Any

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    LifecycleState,
    MemoryTier,
    MemoryUnit,
    MetadataValueType,
    Modality,
    Segment,
    inherited_user_metadata,
)
from jiuwen_memory.construction.base import ExtractContext, OperatorType
from jiuwen_memory.construction.extractor import Extractor, ExtractorProducer

logger = get_logger(__name__)

_CALL_LEVEL_METADATA_KEYS = frozenset({"infer", "pipeline"})


class VideoMemoryExtractor(Extractor):
    """Convert normalized video data into CLM and ELM MemoryUnits."""

    def operator_type(self) -> OperatorType:
        return OperatorType.EXTRACTOR

    def health(self) -> None:
        return None

    def extract(
        self,
        units: list[MemoryUnit],
        *,
        context: ExtractContext | None = None,
    ) -> list[MemoryUnit]:
        del context
        derived: list[MemoryUnit] = []
        for source in units:
            if source.source != Modality.VIDEO:
                continue
            if source.provenance:
                continue
            if source.lifecycle != LifecycleState.ACTIVE:
                continue
            if source.system_metadata.get("modal_type") == "multimodal":
                continue
            try:
                video_data = _load_video_data(source)
            except BackendError as exc:
                logger.warning(
                    "skip non-video memory unit %s during extract: %s",
                    source.id,
                    exc,
                )
                continue
            clips = _build_clips(source, video_data)
            events = _build_events(source, video_data, clips)
            derived.extend(clips.values())
            derived.extend(events)
        return derived


def _build_clips(
    source: MemoryUnit,
    video_data: dict[str, Any],
) -> dict[str, MemoryUnit]:
    video_id = str(video_data.get("payload_id") or source.source_ref)
    clips: dict[str, MemoryUnit] = {}
    for clip in video_data["clips"]:
        source_id = str(clip.get("id", "")).strip()
        if not source_id:
            continue
        if source_id in clips:
            raise BackendError(f"duplicate video clip id: {source_id!r}")
        system_metadata = _memory_system_metadata(
            source,
            level="clm",
            video_id=video_id,
            source_id=source_id,
            start=clip.get("start_seconds"),
            end=clip.get("end_seconds"),
        )
        clips[source_id] = MemoryUnit(
            id=str(uuid.uuid4()),
            scope=source.scope,
            tier=MemoryTier.EPISODIC,
            segments=[
                Segment(
                    content=_memory_content(
                        ("Visual summary", clip.get("visual_summary")),
                        ("Detailed caption", clip.get("detailed_caption")),
                        ("Speech transcript", clip.get("asr")),
                        ("Environment", clip.get("environment")),
                    ),
                    assets=list(source.assets),
                    source=source.source,
                )
            ],
            source_ref=source.source_ref,
            temporal=deepcopy(source.temporal),
            provenance=[source.id],
            tags=list(source.tags),
            system_metadata=system_metadata,
            user_metadata=inherited_user_metadata([source]),
        )
    return clips


def _build_events(
    source: MemoryUnit,
    video_data: dict[str, Any],
    clips: dict[str, MemoryUnit],
) -> list[MemoryUnit]:
    video_id = str(video_data.get("payload_id") or source.source_ref)
    events: list[MemoryUnit] = []
    for event in video_data["events"]:
        source_id = str(event.get("id", "")).strip()
        child_ids = event.get("clip_ids", [])
        if not source_id or not isinstance(child_ids, list):
            continue
        normalized_child_ids = [str(child) for child in child_ids]
        missing = [child for child in normalized_child_ids if child not in clips]
        if missing:
            raise BackendError(
                f"video event {source_id!r} references missing clips: {missing}"
            )
        system_metadata = _memory_system_metadata(
            source,
            level="elm",
            video_id=video_id,
            source_id=source_id,
            start=event.get("start_seconds"),
            end=event.get("end_seconds"),
        )
        system_metadata["child_clm_source_ids"] = list(normalized_child_ids)
        events.append(
            MemoryUnit(
                id=str(uuid.uuid4()),
                scope=source.scope,
                tier=MemoryTier.EPISODIC,
                segments=[
                    Segment(
                        content=_memory_content(
                            ("Topic", event.get("topic")),
                            ("Event summary", event.get("summary")),
                            ("Semantic inference", event.get("semantic_inference")),
                        ),
                        assets=list(source.assets),
                        source=source.source,
                    )
                ],
                source_ref=source.source_ref,
                temporal=deepcopy(source.temporal),
                provenance=[source.id],
                tags=list(source.tags),
                system_metadata=system_metadata,
                user_metadata=inherited_user_metadata([source]),
            )
        )
    return events


def _load_video_data(unit: MemoryUnit) -> dict[str, Any]:
    try:
        value = json.loads(unit.content)
    except json.JSONDecodeError as exc:
        raise BackendError("video memory content is not valid JSON") from exc
    if not isinstance(value, dict):
        raise BackendError("video memory content must be an object")
    for field in ("clips", "events"):
        items = value.get(field)
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise BackendError(f"video memory {field} must be a list of objects")
    return value


def _memory_system_metadata(
    source: MemoryUnit,
    *,
    level: str,
    video_id: str,
    source_id: str,
    start: object,
    end: object,
) -> dict[str, MetadataValueType]:
    metadata = {
        k: v for k, v in source.system_metadata.items()
        if k not in _CALL_LEVEL_METADATA_KEYS
    }
    metadata.update(
        {
            "modal_type": "multimodal",
            "memory_level": level,
            "video_id": video_id,
            "source_memory_id": source_id,
            "start_seconds": _memory_float(start, field=f"{level}.start_seconds"),
            "end_seconds": _memory_float(end, field=f"{level}.end_seconds"),
        }
    )
    return metadata


def _memory_content(*parts: tuple[str, object]) -> str:
    lines = [
        f"{label}: {str(value).strip()}"
        for label, value in parts
        if str(value or "").strip()
    ]
    if not lines:
        raise BackendError("video memory item has no textual content")
    return "\n".join(lines)


def _memory_float(value: object, *, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise BackendError(f"video memory {field} must be numeric") from exc


@ExtractorProducer.register("video_memory")
def _build(config):
    del config
    return VideoMemoryExtractor()
