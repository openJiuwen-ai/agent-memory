# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Video normalizer: raw video reference -> structured video-memory data."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from jiuwen_memory.common.base import PluginType
from jiuwen_memory.common.errors import BackendError, HealthCheckError, ValidationError
from jiuwen_memory.common.llm.base import LLM, LlmProducer
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.normalizer.base import (
    Normalizer,
    NormalizerProducer,
    ensure_normalizer_supports,
)
from jiuwen_memory.common.type_def import Modality, RawPayload

from .video_asr import VideoAsrProducer, VideoAsrService

VideoMemoryOutput = tuple[list[dict[str, Any]], list[dict[str, Any]]]
VideoMemoryBackend = Callable[[RawPayload], VideoMemoryOutput]
DEFAULT_MAX_INLINE_VIDEO_BYTES = 50 * 1024 * 1024

logger = get_logger(__name__)


class VideoNormalizer(Normalizer):
    """Run video perception and return structured video-memory data."""

    def __init__(
        self,
        *,
        chunk_seconds: int = 30,
        asr_port: VideoAsrService | None = None,
        asr_language: str = "",
        asr_chunk_seconds: int = 600,
        llm_port: LLM | None = None,
        vlm_port: LLM | None = None,
        vlm_max_inline_video_bytes: int = DEFAULT_MAX_INLINE_VIDEO_BYTES,
        temp_root: str = "",
        backend: VideoMemoryBackend | None = None,
    ) -> None:
        if chunk_seconds <= 0:
            raise ValidationError("chunk_seconds must be greater than zero")
        if asr_chunk_seconds <= 0:
            raise ValidationError("asr_chunk_seconds must be greater than zero")
        if vlm_max_inline_video_bytes <= 0:
            raise ValidationError("vlm_max_inline_video_bytes must be greater than zero")
        self._chunk_seconds = chunk_seconds
        self._asr_port = asr_port
        self._asr_language = asr_language
        self._asr_chunk_seconds = asr_chunk_seconds
        self._llm_port = llm_port
        self._vlm_port = vlm_port
        self._vlm_max_inline_video_bytes = vlm_max_inline_video_bytes
        self._temp_root = temp_root
        self._backend = backend

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any] | None,
        *,
        asr_port: VideoAsrService | None = None,
        llm_port: LLM | None = None,
        vlm_port: LLM | None = None,
        backend: VideoMemoryBackend | None = None,
    ) -> VideoNormalizer:
        params = dict(config or {})
        return cls(
            chunk_seconds=int(params.get("chunk_seconds", 30)),
            asr_port=asr_port,
            asr_language=str(params.get("asr_language", "")),
            asr_chunk_seconds=int(params.get("asr_chunk_seconds", 600)),
            llm_port=llm_port,
            vlm_port=vlm_port,
            vlm_max_inline_video_bytes=int(
                params.get(
                    "vlm_max_inline_video_bytes", DEFAULT_MAX_INLINE_VIDEO_BYTES
                )
            ),
            temp_root=str(params.get("temp_root", "")),
            backend=backend,
        )

    @staticmethod
    def modalities() -> list[Modality]:
        return [Modality.VIDEO]

    @staticmethod
    def plugin_type() -> PluginType:
        return PluginType.NORMALIZER

    def health(self) -> None:
        if self._backend is not None:
            return
        if self._asr_port is None:
            raise HealthCheckError("video normalizer requires a configured asr_port")
        self._asr_port.health()
        missing_binaries = [
            name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None
        ]
        if missing_binaries:
            raise HealthCheckError(
                "multimodal system dependencies are missing: "
                + ", ".join(missing_binaries)
            )

    def normalize(self, payload: RawPayload) -> str:
        ensure_normalizer_supports(self, payload.modality)
        clips, events = self._extract_video_memory(payload)
        video_memory = {
            "payload_id": payload.id,
            "asset_uri": payload.uri,
            "clips": [_normalize_clip(item) for item in clips],
            "events": [_normalize_event(item) for item in events],
        }
        return json.dumps(video_memory, ensure_ascii=False, separators=(",", ":"))

    def _extract_video_memory(self, payload: RawPayload) -> VideoMemoryOutput:
        if self._backend is not None:
            return self._backend(payload)
        if not payload.uri:
            raise ValidationError("video normalization requires RawPayload.uri")

        video_path = _file_uri_to_path(payload.uri)
        if not video_path.is_file():
            raise ValidationError(f"video file not found: {video_path}")

        temp_root = Path(self._temp_root).expanduser() if self._temp_root else None
        if temp_root is not None:
            try:
                temp_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise BackendError(
                    f"cannot create video temp_root {temp_root}: {exc}"
                ) from exc
            if not temp_root.is_dir():
                raise BackendError(f"video temp_root is not a directory: {temp_root}")
        try:
            temporary = tempfile.TemporaryDirectory(
                prefix="agent-memory-video-",
                dir=temp_root,
            )
        except OSError as exc:
            raise BackendError(
                f"cannot create temporary video directory under {temp_root}: {exc}"
            ) from exc
        with temporary as temp_dir:
            return self._run_pipeline(video_path, Path(temp_dir))

    def _run_pipeline(self, video_path: Path, run_root: Path) -> VideoMemoryOutput:
        try:
            from .video_pipeline import VideoPipelineConfig, run_video_memory_pipeline_off

            outputs = run_video_memory_pipeline_off(
                video_path,
                run_root,
                VideoPipelineConfig(
                    chunk_seconds=self._chunk_seconds,
                    asr_port=self._asr_port,
                    asr_language=self._asr_language or None,
                    asr_chunk_seconds=self._asr_chunk_seconds,
                    require_precomputed_asr=False,
                    llm_port=self._llm_port,
                    vlm_port=self._vlm_port,
                    vlm_max_inline_video_bytes=self._vlm_max_inline_video_bytes,
                    resume_from_stream=False,
                    cleanup=True,
                ),
            )
            return (
                _object_list(outputs.get("short_term"), "short_term"),
                _object_list(outputs.get("medium_term"), "medium_term"),
            )
        except Exception as exc:
            logger.exception(
                "VideoNormalizer: pipeline failed for video=%s", video_path
            )
            raise BackendError(f"embedded video normalization failed: {exc}") from exc


def _normalize_clip(item: dict[str, Any]) -> dict[str, Any]:
    source_id = _required_string(item, "id", "clip")
    start, end = _time_range(item, "time_range", "clip")
    return {
        "id": source_id,
        "start_seconds": start,
        "end_seconds": end,
        "visual_summary": str(item.get("visual_summary", "")).strip(),
        "detailed_caption": str(item.get("detailed_caption", "")).strip(),
        "asr": str(item.get("ASR", "")).strip(),
        "environment": str(item.get("environment", "")).strip(),
    }


def _normalize_event(item: dict[str, Any]) -> dict[str, Any]:
    source_id = _required_string(item, "task_id", "event")
    start, end = _time_range(item, "time_span", "event")
    children = item.get("child_clip_ids", [])
    if not isinstance(children, list):
        raise BackendError("video event child_clip_ids must be a list")
    return {
        "id": source_id,
        "start_seconds": start,
        "end_seconds": end,
        "topic": str(item.get("topic", "")).strip(),
        "summary": str(item.get("narrative_summary", "")).strip(),
        "semantic_inference": str(item.get("semantic_inference", "")).strip(),
        "clip_ids": [str(child) for child in children],
    }


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme not in ("", "file"):
        raise ValidationError(
            f"video normalizer does not support URI scheme {parsed.scheme!r}"
        )
    if parsed.scheme == "":
        return Path(uri).expanduser()
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc not in ("", "localhost"):
        path = f"//{parsed.netloc}{path}"
    is_windows_drive = len(path) >= 3 and path[0] == "/" and path[2] == ":"
    if os.name == "nt" and is_windows_drive:
        path = path[1:]
    return Path(path)


def _object_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise BackendError(f"video memory {label} must be a list of objects")
    return value


def _required_string(item: dict[str, Any], key: str, label: str) -> str:
    value = str(item.get(key, "")).strip()
    if not value:
        raise BackendError(f"video {label} is missing {key}")
    return value


def _time_range(item: dict[str, Any], key: str, label: str) -> tuple[float, float]:
    value = item.get(key)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise BackendError(f"video {label} {key} must contain start and end")
    try:
        start, end = float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise BackendError(f"video {label} {key} must be numeric") from exc
    if end < start:
        raise BackendError(f"video {label} {key} end must be >= start")
    return start, end


@NormalizerProducer.register("video")
def _build(config):
    return VideoNormalizer(
        chunk_seconds=int(config.get("chunk_seconds", 30)),
        asr_port=VideoAsrProducer.dep(config, "asr_port"),
        asr_language=str(config.get("asr_language", "")),
        asr_chunk_seconds=int(config.get("asr_chunk_seconds", 600)),
        llm_port=LlmProducer.dep(config, "llm_port"),
        vlm_port=LlmProducer.dep(config, "vlm_port"),
        vlm_max_inline_video_bytes=int(
            config.get("vlm_max_inline_video_bytes", DEFAULT_MAX_INLINE_VIDEO_BYTES)
        ),
        temp_root=str(config.get("temp_root", "")),
    )
