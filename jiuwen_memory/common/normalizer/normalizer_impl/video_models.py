from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


def _ensure_float_list(values: list[float]) -> list[float]:
    """确保所需资源或状态已就绪。

    Args:
        values: 参数 values（list[float]）。

    Returns:
        返回 list[float]。

    Raises:
        TypeError: 执行失败时抛出。
    """
    if not isinstance(values, list):
        raise TypeError("embedding must be a list of floats")
    out: list[float] = []
    for v in values:
        if isinstance(v, (int, float)):
            out.append(float(v))
        else:
            raise TypeError("embedding must contain only numeric values")
    return out


def _ensure_time_tuple(tp: tuple[float, float]) -> tuple[float, float]:
    """确保所需资源或状态已就绪。

    Args:
        tp: 参数 tp（tuple[float, float]）。

    Returns:
        返回 tuple[float, float]。

    Raises:
        TypeError: 执行失败时抛出。
        ValueError: 执行失败时抛出。
    """
    if not (isinstance(tp, (list, tuple)) and len(tp) == 2):
        raise TypeError("time range/span must be a tuple/list of two numbers")
    start, end = tp
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        raise TypeError("time range/span values must be numeric")
    start_f, end_f = float(start), float(end)
    if end_f < start_f:
        raise ValueError("end time must be >= start time")
    return (start_f, end_f)


@dataclass
class ShortTermMemory:
    """
    Short-Term Memory / Atomic Clip

    Minimal atomic storage unit for a single video clip (e.g., 30s).
    Captures detailed perception and a concise visual summary for similarity.
    """

    id: UUID
    video_source_path: str
    time_range: tuple[float, float]
    visual_summary: str
    detailed_caption: str
    embedding: list[float]
    asr: str
    environment: str
    inferred_intent: str = ""

    def __post_init__(self) -> None:
        """执行 `post_init` 操作。

        Raises:
            TypeError: 执行失败时抛出。
        """
        if not isinstance(self.id, UUID):
            raise TypeError("id must be a UUID")
        if not isinstance(self.video_source_path, str) or not self.video_source_path:
            raise TypeError("video_source_path must be a non-empty string")
        self.time_range = _ensure_time_tuple(self.time_range)
        if not isinstance(self.visual_summary, str):
            raise TypeError("visual_summary must be a string")
        if not isinstance(self.inferred_intent, str):
            raise TypeError("inferred_intent must be a string")
        if not isinstance(self.detailed_caption, str):
            raise TypeError("detailed_caption must be a string")
        self.embedding = _ensure_float_list(self.embedding)
        if not isinstance(self.asr, str):
            raise TypeError("asr must be a string")
        if not isinstance(self.environment, str):
            raise TypeError("environment must be a string")

    def duration(self) -> float:
        """执行 `duration` 操作。

        Returns:
            返回 float。
        """
        start, end = self.time_range
        return float(end - start)

    def to_dict(self) -> dict[str, Any]:
        """执行 `to_dict` 操作。

        Returns:
            返回 dict[str, Any]。
        """
        return {
            "id": str(self.id),
            "video_source_path": self.video_source_path,
            "time_range": [self.time_range[0], self.time_range[1]],
            "visual_summary": self.visual_summary,
            "detailed_caption": self.detailed_caption,
            "embedding": list(self.embedding),
            "ASR": self.asr,
            "environment": self.environment,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ShortTermMemory:
        """执行 `from_dict` 操作。

        Args:
            d: 参数 d（dict[str, Any]）。

        Returns:
            返回 ShortTermMemory。
        """
        return ShortTermMemory(
            id=UUID(d["id"]) if not isinstance(d.get("id"), UUID) else d["id"],
            video_source_path=d["video_source_path"],
            time_range=_ensure_time_tuple(tuple(d["time_range"])),
            visual_summary=d["visual_summary"],
            detailed_caption=d["detailed_caption"],
            embedding=_ensure_float_list(list(d["embedding"])),
            asr=d.get("ASR", ""),
            environment=d.get("environment", ""),
        )


@dataclass
class MediumTermMemory:
    """
    Medium-Term Memory / Task Session

    Represents a complete event or task, aggregating consecutive clips
    into a coherent narrative with evidence via child clip references.
    """

    task_id: UUID
    topic: str
    time_span: tuple[float, float]
    narrative_summary: str
    child_clip_ids: list[UUID]
    embedding: list[float]
    semantic_inference: str = ""

    def __post_init__(self) -> None:
        """执行 `post_init` 操作。

        Raises:
            TypeError: 执行失败时抛出。
        """
        if not isinstance(self.task_id, UUID):
            raise TypeError("task_id must be a UUID")
        if not isinstance(self.topic, str) or not self.topic:
            raise TypeError("topic must be a non-empty string")
        self.time_span = _ensure_time_tuple(self.time_span)
        if not isinstance(self.narrative_summary, str):
            raise TypeError("narrative_summary must be a string")
        if not isinstance(self.semantic_inference, str):
            raise TypeError("semantic_inference must be a string")
        if not isinstance(self.child_clip_ids, list):
            raise TypeError("child_clip_ids must be a list of UUIDs")
        self.child_clip_ids = [
            cid if isinstance(cid, UUID) else UUID(str(cid))
            for cid in self.child_clip_ids
        ]
        self.embedding = _ensure_float_list(self.embedding)

    def duration(self) -> float:
        """执行 `duration` 操作。

        Returns:
            返回 float。
        """
        start, end = self.time_span
        return float(end - start)

    def add_child_clip(self, clip_id: UUID) -> None:
        """执行 `add_child_clip` 操作。

        Args:
            clip_id: 参数 clip_id（UUID）。

        Raises:
            TypeError: 执行失败时抛出。
        """
        if not isinstance(clip_id, UUID):
            raise TypeError("clip_id must be a UUID")
        self.child_clip_ids.append(clip_id)

    def to_dict(self) -> dict[str, Any]:
        """执行 `to_dict` 操作。

        Returns:
            返回 dict[str, Any]。
        """
        return {
            "task_id": str(self.task_id),
            "topic": self.topic,
            "time_span": [self.time_span[0], self.time_span[1]],
            "narrative_summary": self.narrative_summary,
            "semantic_inference": self.semantic_inference,
            "child_clip_ids": [str(cid) for cid in self.child_clip_ids],
            "embedding": list(self.embedding),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> MediumTermMemory:
        """执行 `from_dict` 操作。

        Args:
            d: 参数 d（dict[str, Any]）。

        Returns:
            返回 MediumTermMemory。
        """
        return MediumTermMemory(
            task_id=UUID(d["task_id"])
            if not isinstance(d.get("task_id"), UUID)
            else d["task_id"],
            topic=d["topic"],
            time_span=_ensure_time_tuple(tuple(d["time_span"])),
            narrative_summary=d["narrative_summary"],
            semantic_inference=str(d.get("semantic_inference", "")),
            child_clip_ids=[
                UUID(cid) if not isinstance(cid, UUID) else cid
                for cid in d["child_clip_ids"]
            ],
            embedding=_ensure_float_list(list(d["embedding"])),
        )


__all__ = [
    "ShortTermMemory",
    "MediumTermMemory",
]
