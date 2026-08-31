# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# ruff: noqa: E501

from __future__ import annotations

import atexit
import gc
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from jiuwen_memory.common.llm.base import LLM
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import ChatMessage

from .video_asr import VideoAsrConfig, VideoAsrService, run_video_asr
from .video_models import MediumTermMemory, ShortTermMemory
from .video_prompts import (
    CAPTION_PROMPT,
    EVENT_LINK_WITH_ET_PROMPT_AIO,
    SESSION_SUMMARY_PROMPT_TEMPLATE,
    UPDATE_EVENT_TABLE_PROMPT,
)
from .video_prompts import (
    CHAPTER_CHUNK as CHAPTER_SEGMENT_PROMPT_JSON,
)

# Reduce CUDA fragmentation; must be set before CUDA init
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
# New unified allocator env var (PyTorch >= 2.4)
os.environ.setdefault("PYTORCH_ALLOC_CONF", "expandable_segments:True")

JSON_API_MAX_ATTEMPTS = 3

logger = get_logger(__name__)


@dataclass(frozen=True)
class EventLinkRequest:
    pre_event: dict[str, Any] | None
    anchor: ShortTermMemory
    pending: ShortTermMemory
    segmentation_confidence: str
    chapters: list[dict[str, Any]]
    candidate_source: str


@dataclass(frozen=True)
class SegmentPolicy:
    target_len_s: float = 30.0
    min_len_s: float = 20.0
    max_len_s: float = 40.0
    snap_window_s: float = 10.0


@dataclass(frozen=True)
class VideoPipelineConfig:
    chunk_seconds: int = 30
    asr_port: VideoAsrService | None = None
    asr_language: str | None = None
    asr_chunk_seconds: int = 600
    require_precomputed_asr: bool = False
    llm_port: LLM | None = None
    vlm_port: LLM | None = None
    resume_from_stream: bool = True
    cleanup: bool = True

UPDATE_EVENT_TABLE_PROMPT_FORCE_CONTINUE = (
    UPDATE_EVENT_TABLE_PROMPT
    + """
    # FORCE CONTINUE MODE (HARD)
    - You MUST output delta in CONTINUE format.
    - You MUST NOT output SHIFT under any circumstance.
    - You MAY slightly adjust event_title (event_identity) to appropriately encompass the current state of the ongoing event, while keeping it consistent with the core semantic of pre_ET.event_title.
    - The adjusted event_title must remain local, stable, and not expand to a global/session-level scope; it should only refine the original title to fit cumulative information of the same unit.
    """
)


def _cleanup_dist() -> None:
    """Best-effort destroy any torch distributed/NCCL process group at exit."""
    try:
        import torch.distributed as dist
    except ImportError:
        return
    try:
        if dist.is_available() and dist.is_initialized():
            dist.destroy_process_group()
    except RuntimeError as exc:
        logger.warning("VideoPipeline: failed to destroy process group: %s", exc)


atexit.register(_cleanup_dist)


def _parse_json_safe(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start:end + 1]
            try:
                return json.loads(candidate)
            except (TypeError, json.JSONDecodeError):
                pass
        raise ValueError("Model did not return valid JSON.") from exc


def _raw_snippet(text: Any, limit: int = 200) -> str:
    snippet = str(text or "").replace("\n", " ").strip()
    if len(snippet) > limit:
        return snippet[:limit] + "..."
    return snippet


def _call_openai_json_with_retry(
    *,
    llm_port: LLM,
    prompt: str,
    max_tokens: int,
    temperature: float | None = None,
    validator: Any | None = None,
    call_name: str = "json_call",
) -> tuple[dict[str, Any] | None, str, str]:
    last_error = ""
    last_raw = ""

    for attempt in range(1, JSON_API_MAX_ATTEMPTS + 1):
        retry_prompt = prompt
        if attempt > 1:
            retry_prompt = (
                f"{prompt}\n\n"
                "Your previous response could not be consumed by the pipeline. "
                "Return valid JSON only, matching the requested schema exactly. "
                "Do not include explanations, markdown fences, comments, or extra keys."
            )

        options: dict[str, Any] = {"max_tokens": max_tokens}
        if temperature is not None:
            options["temperature"] = temperature

        try:
            raw = llm_port.chat(
                [ChatMessage(role="user", content=retry_prompt)],
                **options,
            ).strip()
            last_raw = raw
            data = _parse_json_safe(raw)
            if validator is not None:
                validator(data)
            return data, raw, ""
        except Exception as e:
            last_error = str(e)
            logger.warning(
                "VideoPipeline: %s failed (attempt %d/%d): %s; raw=%s",
                call_name,
                attempt,
                JSON_API_MAX_ATTEMPTS,
                last_error,
                _raw_snippet(last_raw or e),
            )

    return None, last_raw, last_error


def _validate_summary_payload(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("summary payload is not a dict")
    required = ("topic_label", "full_narrative", "semantic_inference")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"summary payload missing keys: {missing}")


def _validate_event_link_payload(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("event link payload is not a dict")
    if "is_same_event" not in data:
        raise ValueError("event link payload missing is_same_event")
    if not isinstance(data.get("is_same_event"), bool):
        raise ValueError("event link payload is_same_event must be bool")
    split_raw = data.get("split_point")
    if split_raw is None:
        split_raw = data.get("split_points", [])
    if split_raw is not None and not isinstance(split_raw, list):
        raise ValueError("event link payload split_point(s) must be list")


def _validate_chapter_payload(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("chapter payload is not a dict")
    chapters = data.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("chapter payload missing non-empty chapters list")
    seg_conf = str(data.get("segmentation_confidence", "")).strip().lower()
    if seg_conf not in {"high", "medium", "low"}:
        raise ValueError(f"invalid segmentation_confidence: {seg_conf}")
    for idx, ch in enumerate(chapters, start=1):
        if not isinstance(ch, dict):
            raise ValueError(f"chapter {idx} is not a dict")
        missing = [k for k in ("start_time", "title", "summary") if k not in ch]
        if missing:
            raise ValueError(f"chapter {idx} missing keys: {missing}")


def _normalize_chapters_start_only(
    chapters: list[dict[str, Any]],
    video_duration_s: float,
) -> list[dict[str, Any]]:
    """Normalize and keep chapter fields in start-time-only format."""
    normalized: list[dict[str, Any]] = []
    for idx, ch in enumerate(chapters, start=1):
        if not isinstance(ch, dict):
            continue
        st_raw = str(ch.get("start_time", "00:00:00"))
        st_s = _hhmmss_to_seconds(st_raw)
        st_s = max(0.0, min(st_s, max(0.0, float(video_duration_s))))
        title = str(ch.get("title", "")).strip() or f"Chapter {idx}"
        summary = str(ch.get("summary", "")).strip()
        normalized.append(
            {
                "chapter_id": int(ch.get("chapter_id", idx) or idx),
                "start_time": _seconds_to_hhmmss(st_s),
                "title": title,
                "summary": summary,
            }
        )

    if not normalized:
        return [
            {
                "chapter_id": 1,
                "start_time": _seconds_to_hhmmss(0.0),
                "title": "Full Video",
                "summary": "Auto-generated chapter due to missing ASR/segmentation",
            }
        ]

    normalized.sort(
        key=lambda c: _hhmmss_to_seconds(str(c.get("start_time", "00:00:00")))
    )

    deduped: list[dict[str, Any]] = []
    for ch in normalized:
        st = _hhmmss_to_seconds(str(ch.get("start_time", "00:00:00")))
        if deduped:
            prev_st = _hhmmss_to_seconds(str(deduped[-1].get("start_time", "00:00:00")))
            if abs(st - prev_st) <= 1e-6:
                # Keep the richer chapter content if starts are duplicated.
                prev_summary = str(deduped[-1].get("summary", "")).strip()
                curr_summary = str(ch.get("summary", "")).strip()
                if len(curr_summary) > len(prev_summary):
                    deduped[-1] = ch
                continue
        deduped.append(ch)

    if _hhmmss_to_seconds(str(deduped[0].get("start_time", "00:00:00"))) > 0.0:
        deduped[0]["start_time"] = _seconds_to_hhmmss(0.0)

    for i, ch in enumerate(deduped, start=1):
        ch["chapter_id"] = i

    return deduped


def _validate_event_table_payload(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ValueError("event table payload is not a dict")
    required = {
        "event_identity",
        "event_summary",
        "entities",
        "open_questions",
        "delta",
    }
    if not required.issubset(set(data.keys())):
        missing = sorted(required - set(data.keys()))
        raise ValueError(f"event table payload missing keys: {missing}")
    identity = str(data.get("event_identity") or data.get("event_intent") or "").strip()
    if not identity:
        raise ValueError("event table payload missing event_identity")
    if not isinstance(data.get("entities"), list):
        raise ValueError("event table payload entities must be list")
    if not isinstance(data.get("open_questions"), list):
        raise ValueError("event table payload open_questions must be list")


def _fill_prompt_template(template: str, **kwargs: Any) -> str:
    """Fill {name} placeholders without interpreting unrelated JSON braces."""
    out = str(template)
    for key, value in kwargs.items():
        out = out.replace("{" + str(key) + "}", str(value))
    return out


def _resolve_video_path(video_path: Path) -> Path:
    """Resolve migrated dataset paths by searching common roots and name variants."""
    if video_path.exists() and video_path.is_file():
        return video_path

    suffix = video_path.suffix or ".mp4"
    stem = video_path.stem if video_path.suffix else video_path.name
    base_name = video_path.name if video_path.suffix else f"{stem}{suffix}"

    stem_variants = [stem]
    if stem.startswith("_"):
        stem_variants.append(stem.lstrip("_"))
    else:
        stem_variants.append(f"_{stem}")

    name_variants: list[str] = [base_name]
    for s in stem_variants:
        n = f"{s}{suffix}"
        if n not in name_variants:
            name_variants.append(n)

    parent = video_path.parent
    if parent.exists() and parent.is_dir():
        for name in name_variants:
            cand = parent / name
            if cand.exists() and cand.is_file():
                logger.info(
                    "VideoPipeline: resolved video path %s -> %s", video_path, cand
                )
                return cand

    raise FileNotFoundError(
        f"Input video not found: {video_path}. Checked common dataset roots and filename variants: {name_variants}"
    )


def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
    def _drop_embeddings(obj: Any) -> Any:
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if k == "embedding":
                    continue
                out[k] = _drop_embeddings(v)
            return out
        if isinstance(obj, list):
            return [_drop_embeddings(x) for x in obj]
        return obj

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _drop_embeddings(item) if path.name.endswith(".stream.jsonl") else item
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


# -------------------- Video helpers --------------------


def _get_clip_duration(clip_path: Path) -> float:
    import shutil
    import subprocess

    if shutil.which("ffprobe") is None:
        raise FileNotFoundError("ffprobe not found in PATH. Please install ffmpeg.")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(clip_path),
    ]
    res = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    out = res.stdout.decode().strip()
    try:
        return float(out)
    except (TypeError, ValueError) as e:
        raise RuntimeError(f"Failed to parse duration for {clip_path}: {out}") from e


def _mmss_to_seconds(ts: str) -> float:
    try:
        parts = ts.split(":")
        if len(parts) != 2:
            return 0.0
        m, s = parts
        return float(m) * 60 + float(s)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _hhmmss_to_seconds(ts: str) -> float:
    try:
        parts = ts.split(":")
        if len(parts) == 2:
            return _mmss_to_seconds(ts)
        if len(parts) != 3:
            return 0.0
        h, m, s = parts
        return float(h) * 3600 + float(m) * 60 + float(s)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _seconds_to_hhmmss(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _normalize_to_hhmmss(ts: str) -> str:
    return _seconds_to_hhmmss(_hhmmss_to_seconds(ts))


# -------------------- Session / memory helpers --------------------


def _summarize_session(
    details: list[str],
    *,
    llm_port: LLM,
) -> dict[str, str]:
    detail_list = "\n".join(f"- {d}" for d in details)
    prompt = _fill_prompt_template(
        SESSION_SUMMARY_PROMPT_TEMPLATE, detail_list=detail_list
    )
    data, raw, _ = _call_openai_json_with_retry(
        llm_port=llm_port,
        prompt=prompt,
        max_tokens=2048,
        validator=_validate_summary_payload,
        call_name="summarize_session",
    )
    if data is None:
        return {
            "topic_label": "",
            "full_narrative": raw,
            "semantic_inference": "",
        }
    return {
        "topic_label": str(data.get("topic_label", "")).strip(),
        "full_narrative": str(data.get("full_narrative", "")).strip(),
        "semantic_inference": str(data.get("semantic_inference", "")).strip(),
    }


def _build_event_link_prompt(request: EventLinkRequest) -> str:
    pre_et_payload = _event_link_pre_et_payload(request.pre_event)
    pre_et_text = (
        "null"
        if not pre_et_payload
        else json.dumps(pre_et_payload, ensure_ascii=False, indent=2)
    )
    seg_conf = str(request.segmentation_confidence or "").strip().lower() or "unknown"

    chapter_context_lines: list[str] = [f"segmentation_confidence: {seg_conf}"]
    if request.chapters:
        chapter_context_lines.append(
            "chapter_list:\n"
            + json.dumps(request.chapters, ensure_ascii=False, indent=2)
        )
    else:
        chapter_context_lines.append("chapter_list: []")

    chapter_context_text = "\n\n".join(chapter_context_lines)

    base = EVENT_LINK_WITH_ET_PROMPT_AIO
    base = base.replace(
        "{candidate_source}",
        str(request.candidate_source or "").strip(),
    )
    base = base.replace("{asr_confidence}", seg_conf)
    base = base.replace("{chapter_context}", chapter_context_text)
    base = base.replace("{pre_et}", pre_et_text)
    base = base.replace("{anchor_summary}", request.anchor.visual_summary)
    base = base.replace("{anchor_caption}", request.anchor.detailed_caption)
    base = base.replace("{anchor_ASR}", request.anchor.asr)
    base = base.replace("{pending_summary}", request.pending.visual_summary)
    base = base.replace("{pending_caption}", request.pending.detailed_caption)
    base = base.replace("{pending_ASR}", request.pending.asr)
    return base


def _event_link_pre_et_payload(
    pre_et: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(pre_et, dict):
        return None
    return {
        "event_identity": pre_et.get("event_identity")
        or pre_et.get("event_intent", ""),
        "event_summary": pre_et.get("event_summary", ""),
        "entities": pre_et.get("entities") or [],
    }


def _event_link_current_segments_payload(
    anchor_stm: ShortTermMemory,
    pending_stm: ShortTermMemory,
) -> dict[str, Any]:
    return {
        "anchor_segment": {
            "clip_id": str(anchor_stm.id),
            "time_range": [anchor_stm.time_range[0], anchor_stm.time_range[1]],
            "summary": anchor_stm.visual_summary,
            "caption": anchor_stm.detailed_caption,
            "asr": anchor_stm.asr,
        },
        "pending_segment": {
            "clip_id": str(pending_stm.id),
            "time_range": [pending_stm.time_range[0], pending_stm.time_range[1]],
            "summary": pending_stm.visual_summary,
            "caption": pending_stm.detailed_caption,
            "asr": pending_stm.asr,
        },
    }


def _parse_split_points(data: dict[str, Any]) -> list[dict[str, str]]:
    points: list[dict[str, str]] = []
    if not isinstance(data, dict):
        return points
    raw = data.get("split_point")
    if raw is None:
        raw = data.get("split_points", [])
    if not isinstance(raw, list):
        return points
    for item in raw:
        if not isinstance(item, dict):
            continue
        t = str(item.get("t", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if t:
            points.append({"t": t, "reason": reason})
    return points


def _format_et_shift_candidate(delta: str, evidence: str) -> str:
    raw = str(delta or "").strip()
    if not raw:
        return f"ET shift:SHIFT: unknown -> unknown | evidence={evidence}."

    upper = raw.upper()
    if upper.startswith("SHIFT:"):
        body = raw
    else:
        body = f"SHIFT: {raw}"

    marker = "| evidence="
    idx = body.lower().find(marker)
    if idx != -1:
        body = body[:idx].rstrip()

    if body.endswith("."):
        body = body[:-1].rstrip()

    return f"ET shift:{body} | evidence={evidence}."


def _is_initialization_shift(delta: str) -> bool:
    """Detect SHIFT deltas that are initialization-like (null/none/empty -> event)."""
    raw = str(delta or "").strip()
    if not raw:
        return False
    upper = raw.upper()
    if not upper.startswith("SHIFT:"):
        return False
    body = raw.split(":", 1)[1].strip() if ":" in raw else ""
    if "->" not in body:
        return False
    lhs = body.split("->", 1)[0].strip().strip(":").strip()
    if not lhs:
        return True
    lhs_token = re.sub(r"[^A-Z0-9]+", "", lhs.upper())
    init_tokens = {
        "NONE",
        "NULL",
        "NIL",
        "NA",
        "NAN",
        "UNKNOWN",
        "UNSET",
        "EMPTY",
        "INITIAL",
        "INITIALSTATE",
        "INIT",
        "NOEVENT",
    }
    return lhs_token in init_tokens


def _judge_event_with_et(
    request: EventLinkRequest,
    *,
    llm_port: LLM,
    log_path: Path | None = None,
    meta: dict[str, Any] | None = None,
) -> tuple[bool, list[dict[str, str]]]:
    pre_et_payload = _event_link_pre_et_payload(request.pre_event)
    current_segments_payload = _event_link_current_segments_payload(
        request.anchor,
        request.pending,
    )
    prompt = _build_event_link_prompt(request)
    data, content, err = _call_openai_json_with_retry(
        llm_port=llm_port,
        prompt=prompt,
        max_tokens=256,
        validator=_validate_event_link_payload,
        call_name="judge_event_with_et",
    )
    if data is None:
        if log_path:
            _append_jsonl(
                log_path,
                {
                    "ts": datetime.utcnow().isoformat() + "Z",
                    "meta": meta or {},
                    "candidate_source": request.candidate_source,
                    "pre_ET": pre_et_payload,
                    "current_segments": current_segments_payload,
                    "raw_response": content,
                    "error": err or "judge_event_with_et failed after retries",
                },
            )
        logger.warning(
            "VideoPipeline: event-link inference exhausted retries; raw=%s",
            _raw_snippet(content),
        )
        return False, []
    try:
        if log_path:
            _append_jsonl(
                log_path,
                {
                    "ts": datetime.utcnow().isoformat() + "Z",
                    "meta": meta or {},
                    "candidate_source": request.candidate_source,
                    "pre_ET": pre_et_payload,
                    "current_segments": current_segments_payload,
                    "raw_response": content,
                    "parsed": data,
                },
            )
        return bool(data.get("is_same_event")), _parse_split_points(data)
    except (AttributeError, TypeError, ValueError) as e:
        if log_path:
            _append_jsonl(
                log_path,
                {
                    "ts": datetime.utcnow().isoformat() + "Z",
                    "meta": meta or {},
                    "candidate_source": request.candidate_source,
                    "pre_ET": pre_et_payload,
                    "current_segments": current_segments_payload,
                    "raw_response": content,
                    "error": str(e),
                },
            )
        logger.warning(
            "VideoPipeline: event-link response parsing failed: %s; raw=%s",
            e,
            _raw_snippet(content),
        )
        return False, []


def _stm_to_event_payload(stm: ShortTermMemory) -> dict[str, Any]:
    return {
        "clip_id": str(stm.id),
        "time_range": [stm.time_range[0], stm.time_range[1]],
        "visual_summary": stm.visual_summary,
        "detailed_caption": stm.detailed_caption,
        "ASR": stm.asr,
        "environment": stm.environment,
    }


def _fallback_event_table(
    pre_et: dict[str, Any] | None, stm: ShortTermMemory
) -> dict[str, Any]:
    identity = (stm.visual_summary or "ongoing activity").strip()
    summary = stm.detailed_caption.strip() or stm.visual_summary.strip() or ""
    if pre_et and isinstance(pre_et, dict):
        return {
            "event_identity": pre_et.get("event_identity")
            or pre_et.get("event_intent")
            or identity,
            "event_summary": pre_et.get("event_summary") or summary,
            "entities": pre_et.get("entities") or [],
            "open_questions": pre_et.get("open_questions") or [],
            "delta": "No update due to parse failure.",
        }
    return {
        "event_identity": identity,
        "event_summary": summary,
        "entities": [],
        "open_questions": [],
        "delta": "Initialize event from current clip.",
    }


def _coerce_event_table(
    data: Any, pre_et: dict[str, Any] | None, stm: ShortTermMemory
) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _fallback_event_table(pre_et, stm)
    required = {"event_summary", "entities", "open_questions", "delta"}
    if not required.issubset(set(data.keys())):
        return _fallback_event_table(pre_et, stm)
    identity = str(data.get("event_identity") or data.get("event_intent") or "").strip()
    if not identity:
        return _fallback_event_table(pre_et, stm)
    if not isinstance(data.get("entities"), list) or not isinstance(
        data.get("open_questions"), list
    ):
        return _fallback_event_table(pre_et, stm)
    return {
        "event_identity": identity,
        "event_summary": str(data.get("event_summary", "")).strip(),
        "entities": data.get("entities") or [],
        "open_questions": data.get("open_questions") or [],
        "delta": str(data.get("delta", "")).strip(),
    }


def _update_event_table(
    pre_et: dict[str, Any] | None,
    stm: ShortTermMemory,
    force_continue: bool = False,
    *,
    llm_port: LLM,
) -> dict[str, Any]:
    pre_et_text = (
        "null" if not pre_et else json.dumps(pre_et, ensure_ascii=False, indent=2)
    )
    stm_text = json.dumps(_stm_to_event_payload(stm), ensure_ascii=False, indent=2)
    base_prompt = (
        UPDATE_EVENT_TABLE_PROMPT_FORCE_CONTINUE
        if force_continue
        else UPDATE_EVENT_TABLE_PROMPT
    )
    prompt = f"{base_prompt}\n\npre_ET:\n{pre_et_text}\n\nSTM_k:\n{stm_text}"
    data, content, err = _call_openai_json_with_retry(
        llm_port=llm_port,
        prompt=prompt,
        max_tokens=1024,
        validator=_validate_event_table_payload,
        call_name="update_event_table",
    )
    if data is None:
        logger.warning(
            "VideoPipeline: event-table inference exhausted retries: %s; raw=%s",
            err,
            _raw_snippet(content),
        )
        return _fallback_event_table(pre_et, stm)
    return _coerce_event_table(data, pre_et, stm)


# -------------------- Chapter segmentation --------------------


def _segment_chapters(
    cleaned_asr_segments: list[dict[str, str]],
    video_duration_s: float,
    *,
    llm_port: LLM,
) -> dict[str, Any]:
    chapters_only_schema = (
        "You must return JSON only in the following schema:\n"
        "{\n"
        '  "segmentation_confidence": string,  // one of: high, medium, low\n'
        '  "chapters": [\n'
        "    {\n"
        '      "chapter_id": number,\n'
        '      "start_time": "HH:MM:SS",\n'
        '      "title": string,\n'
        '      "summary": string\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )
    transcript_lines = []
    for seg in cleaned_asr_segments:
        st = _normalize_to_hhmmss(seg.get("start", "00:00"))
        text = str(seg.get("text", "")).strip()
        transcript_lines.append(f"[{st}] {text}")

    if not transcript_lines:
        raise RuntimeError(
            "ASR chaptering aborted: no ASR transcript lines available for chapter segmentation."
        )

    prompt = f"{CHAPTER_SEGMENT_PROMPT_JSON}\n\n{chapters_only_schema}\n\n" + "\n".join(
        transcript_lines
    )
    data, _, err = _call_openai_json_with_retry(
        llm_port=llm_port,
        prompt=prompt,
        max_tokens=8000,
        temperature=0,
        validator=_validate_chapter_payload,
        call_name="segment_chapters",
    )
    if data is not None:
        seg_conf = (
            str(data.get("segmentation_confidence", "")).strip().lower() or "unknown"
        )
        chapters = _normalize_chapters_start_only(
            data.get("chapters") or [], video_duration_s
        )
        return {"chapters": chapters, "segmentation_confidence": seg_conf}
    raise RuntimeError(
        "ASR chaptering failed after "
        f"{JSON_API_MAX_ATTEMPTS} attempts; aborting this task. "
        f"error={err}; asr_segments={len(cleaned_asr_segments)}"
    )


def _should_fallback_to_legacy(
    asr_segments: list[dict[str, str]],
    cleaned_asr_segments: list[dict[str, str]],
    chapters: list[dict[str, Any]],
    video_duration_s: float,
    segmentation_confidence: str | None = None,
) -> tuple[bool, str]:
    try:
        if segmentation_confidence and segmentation_confidence.lower() in ("low"):
            return (
                True,
                f"chapter segmentation_confidence {segmentation_confidence.lower()}",
            )

        text_len = sum(len(str(seg.get("text", ""))) for seg in cleaned_asr_segments)
        if not asr_segments or not cleaned_asr_segments or text_len < 200:
            return True, "ASR empty or too short"

        if not chapters:
            return True, "chaptering returned empty"

        durations = []
        titles_norm: list[str] = []
        for idx, ch in enumerate(chapters):
            st = _hhmmss_to_seconds(str(ch.get("start_time", "00:00:00")))
            if idx + 1 < len(chapters):
                ed = _hhmmss_to_seconds(
                    str(chapters[idx + 1].get("start_time", "00:00:00"))
                )
            else:
                ed = video_duration_s
            st = max(0.0, min(st, video_duration_s))
            ed = max(st, min(ed, video_duration_s))
            durations.append(ed - st)
            title = str(ch.get("title", "")).strip().lower()
            titles_norm.append(" ".join(title.split()))

        if len(chapters) == 1 and video_duration_s >= 600:
            return True, "single chapter for long video"

        # 注释掉章节过长的回退判断
        # if durations and max(durations) > 900:
        #     return True, "chapter too long"

        if titles_norm:
            from collections import Counter

            ctr = Counter(titles_norm)
            most_common = ctr.most_common(1)[0][1]
            if most_common / max(len(titles_norm), 1) >= 0.70:
                return True, "chapter titles repetitive"

    except (AttributeError, IndexError, TypeError, ValueError) as e:
        return True, f"chapter validation error: {e}"

    return False, "ok"


def _collect_asr_boundaries(cleaned_asr_segments: list[dict[str, str]]) -> list[float]:
    boundaries: list[float] = []
    for seg in cleaned_asr_segments:
        st = _hhmmss_to_seconds(seg.get("start", "00:00:00"))
        ed = _hhmmss_to_seconds(seg.get("end", "00:00:00"))
        if st >= 0:
            boundaries.append(st)
        if ed >= 0:
            boundaries.append(ed)
    return sorted(set(boundaries))


def _collect_chapter_boundaries(chapters: list[dict[str, Any]]) -> list[float]:
    boundaries: list[float] = []
    for idx, ch in enumerate(chapters):
        if idx == 0:
            continue
        st = _hhmmss_to_seconds(str(ch.get("start_time", "00:00:00")))
        if st >= 0:
            boundaries.append(st)
    return sorted(set(boundaries))


def _chapter_time_span(
    chapters: list[dict[str, Any]],
    index: int,
    video_duration_s: float,
) -> tuple[float, float]:
    """Derive chapter [start, end) from start-only chapter list."""
    start_s = _hhmmss_to_seconds(str(chapters[index].get("start_time", "00:00:00")))
    if index + 1 < len(chapters):
        end_s = _hhmmss_to_seconds(
            str(chapters[index + 1].get("start_time", "00:00:00"))
        )
    else:
        end_s = max(0.0, float(video_duration_s))
    start_s = max(0.0, min(start_s, max(0.0, float(video_duration_s))))
    end_s = max(start_s, min(end_s, max(0.0, float(video_duration_s))))
    return start_s, end_s


def make_asr_aligned_segments(
    start_s: float,
    end_s: float,
    boundaries: list[float],
    policy: SegmentPolicy = SegmentPolicy(),
) -> list[dict[str, float]]:
    segments: list[dict[str, float]] = []
    cur = start_s
    while cur < end_s - 1e-6:
        remaining = end_s - cur
        if remaining <= 5.0:
            if segments:
                segments[-1]["end_s"] = end_s
            else:
                segments.append({"segment_id": 1, "start_s": start_s, "end_s": end_s})
            break

        proposed = min(end_s, cur + policy.target_len_s)
        candidates: list[tuple[float, float]] = []  # (distance, boundary)
        for b in boundaries:
            if b <= cur or b >= end_s:
                continue
            seg_len = b - cur
            if seg_len < policy.min_len_s or seg_len > policy.max_len_s:
                continue
            if abs(b - proposed) <= policy.snap_window_s:
                candidates.append((abs(b - proposed), b))

        chosen = proposed
        if candidates:
            candidates.sort(key=lambda x: x[0])
            chosen = candidates[0][1]

        seg_end = min(end_s, chosen)
        if seg_end - cur < policy.min_len_s and remaining > policy.min_len_s:
            seg_end = min(end_s, cur + policy.min_len_s)
        if seg_end - cur > policy.max_len_s:
            seg_end = min(end_s, cur + policy.max_len_s)

        if seg_end - cur < 5.0 and segments:
            segments[-1]["end_s"] = end_s
            break

        segments.append(
            {"segment_id": len(segments) + 1, "start_s": cur, "end_s": seg_end}
        )
        cur = seg_end

    return segments


def _get_video_duration(path: Path) -> float:
    return _get_clip_duration(path)


def _extract_video_subclip(
    video_path: Path,
    out_path: Path,
    start_s: float,
    end_s: float,
    timeout_s: int = 180,
) -> Path:
    import shutil
    import subprocess

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg") is None:
        raise FileNotFoundError("ffmpeg not found in PATH. Please install ffmpeg.")
    start_s = max(0.0, float(start_s))
    end_s = max(start_s, float(end_s))
    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_s:.3f}",
        "-to",
        f"{end_s:.3f}",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(out_path),
    ]
    try:
        subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=int(timeout_s),
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"ffmpeg subclip timeout after {timeout_s}s: [{start_s:.3f}, {end_s:.3f}] {video_path}"
        ) from e
    return out_path


def _asr_for_range(
    asr_segments: list[dict[str, str]], start_s: float, end_s: float
) -> list[str]:
    lines: list[str] = []
    for seg in asr_segments:
        seg_start = _hhmmss_to_seconds(seg.get("start", "00:00:00"))
        seg_end = _hhmmss_to_seconds(seg.get("end", "00:00:00"))
        # Use half-open interval [start_s, end_s) to avoid boundary duplication
        if seg_end <= start_s or seg_start >= end_s:
            continue
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        st_norm = _normalize_to_hhmmss(seg.get("start", "00:00:00"))
        ed_norm = _normalize_to_hhmmss(seg.get("end", "00:00:00"))
        lines.append(f"[{st_norm}-{ed_norm}]:{text}")
    return lines


def _merge_short_segments(
    segments: list[dict[str, Any]],
    min_len_s: float,
    chapter_start_s: float,
    chapter_end_s: float,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for seg in segments:
        st_s = max(chapter_start_s, float(seg.get("start_s", 0.0)))
        ed_s = min(chapter_end_s, float(seg.get("end_s", st_s)))
        if ed_s < st_s:
            st_s, ed_s = ed_s, ed_s
        dur = ed_s - st_s
        if dur < min_len_s and merged:
            merged[-1]["end_s"] = max(merged[-1]["end_s"], ed_s)
        else:
            merged.append(
                {"segment_id": seg.get("segment_id"), "start_s": st_s, "end_s": ed_s}
            )
    return merged


# -------------------- Offline Qwen-VL inference --------------------


def _offline_vu(
    clip_path: Path,
    prompt: str,
    llm_port: LLM,
) -> dict[str, Any]:
    """Run vision-language inference through the configured LLM plugin."""
    max_retries = 4
    generated_text = ""
    for attempt in range(max_retries + 1):
        try:
            retry_prompt = (
                prompt
                if attempt == 0
                else (
                    f"{prompt}\n\nPlease strictly output valid JSON only. Do not include any extra text."
                )
            )
            generated_text = llm_port.chat(
                [
                    ChatMessage(
                        role="user",
                        content=[
                            {
                                "type": "video_url",
                                "video_url": {"url": clip_path.resolve().as_uri()},
                            },
                            {"type": "text", "text": retry_prompt},
                        ],
                    )
                ],
                max_tokens=512,
                temperature=0.4,
            ).strip()
            return _parse_json_safe(generated_text)
        except Exception as e:
            snippet = (generated_text or str(e)).replace("\n", " ")
            logger.warning(
                "VideoPipeline: VLM inference failed (attempt %d/%d): %s",
                attempt + 1,
                max_retries + 1,
                _raw_snippet(snippet),
            )
            if attempt < max_retries:
                continue
            raise RuntimeError("VLM inference failed after retries") from e


# -------------------- Main pipeline --------------------


def run_video_memory_pipeline_off(
    video_path: str | Path,
    work_root: str | Path,
    config: VideoPipelineConfig,
) -> dict[str, Any]:
    video_path = _resolve_video_path(Path(video_path))
    chunk_seconds = config.chunk_seconds
    asr_port = config.asr_port
    asr_language = config.asr_language
    asr_chunk_seconds = config.asr_chunk_seconds
    require_precomputed_asr = config.require_precomputed_asr
    llm_port = config.llm_port
    vlm_port = config.vlm_port
    resume_from_stream = config.resume_from_stream
    cleanup = config.cleanup
    if asr_port is None:
        raise ValueError("configure asr_port in the video normalizer")
    if llm_port is None:
        raise ValueError("configure llm_port in the video normalizer")
    if vlm_port is None:
        raise ValueError("configure vlm_port in the video normalizer")
    logger.info(
        "VideoPipeline: starting video=%s work_root=%s chunk_seconds=%d",
        video_path,
        work_root,
        chunk_seconds,
    )
    work_root = Path(work_root)
    segments_dir = work_root / "segments"
    outputs_dir = work_root / "outputs"
    segments_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    event_link_log_path = outputs_dir / "event_link_logs.jsonl"
    st_stream_path = outputs_dir / "short_term.stream.jsonl"
    mt_stream_path = outputs_dir / "medium_term.stream.jsonl"
    et_stream_path = outputs_dir / "event_table.stream.jsonl"
    resume_active = bool(resume_from_stream) and any(
        p.exists() and p.is_file() and p.stat().st_size > 0
        for p in (st_stream_path, mt_stream_path, et_stream_path)
    )

    if resume_active:
        logger.info("VideoPipeline: resuming from existing stream state")
    else:
        with open(event_link_log_path, "w", encoding="utf-8") as f:
            f.write("")
        for p in (st_stream_path, mt_stream_path, et_stream_path):
            with open(p, "w", encoding="utf-8") as f:
                f.write("")

    # Ensure files exist for append mode even when partially resumed.
    for p in (st_stream_path, mt_stream_path, et_stream_path, event_link_log_path):
        if not p.exists():
            with open(p, "w", encoding="utf-8") as f:
                f.write("")

    # ASR once via UASR, write only asr_segments.json
    asr_save_path = outputs_dir / "asr_segments.json"
    asr_tmp_work_dir = work_root / "asr_tmp"
    asr_segments: list[dict[str, str]] = []
    reused_asr = False
    if asr_save_path.exists() and asr_save_path.is_file():
        try:
            with open(asr_save_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, list) and cached:
                asr_segments = cached
                reused_asr = True
                logger.info(
                    "VideoPipeline: reusing ASR transcript path=%s segments=%d",
                    asr_save_path,
                    len(asr_segments),
                )
            elif isinstance(cached, list) and not cached:
                logger.warning(
                    "VideoPipeline: cached ASR transcript is empty; rerunning path=%s",
                    asr_save_path,
                )
            else:
                logger.warning(
                    "VideoPipeline: cached ASR transcript is invalid; rerunning path=%s",
                    asr_save_path,
                )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "VideoPipeline: failed to load cached ASR transcript; rerunning: %s",
                e,
            )

    if require_precomputed_asr and not reused_asr:
        raise RuntimeError(
            f"ASR prerequisite missing or empty: {asr_save_path}. Run batch_ASR first and retry."
        )

    if not reused_asr:
        asr_segments = run_video_asr(
            video_path,
            VideoAsrConfig(
                service=asr_port,
                language=asr_language,
                chunk_seconds=max(1, int(asr_chunk_seconds)),
                output_json=asr_save_path,
                temp_work_dir=asr_tmp_work_dir,
                cleanup=True,
            ),
        )
        logger.info(
            "VideoPipeline: ASR completed path=%s segments=%d",
            asr_save_path,
            len(asr_segments),
        )

    video_duration_s = _get_video_duration(video_path)

    chapter_out_path = outputs_dir / "chapter_segmentation.json"
    chapter_json: dict[str, Any] = {}
    reused_chapters = False
    if chapter_out_path.exists() and chapter_out_path.is_file():
        try:
            with open(chapter_out_path, "r", encoding="utf-8") as f:
                cached_chapters = json.load(f)
            if isinstance(cached_chapters, dict) and isinstance(
                cached_chapters.get("chapters"), list
            ):
                chapter_json = cached_chapters
                reused_chapters = True
                logger.info(
                    "VideoPipeline: reusing chapter segmentation path=%s chapters=%d",
                    chapter_out_path,
                    len(cached_chapters.get("chapters", [])),
                )
            else:
                logger.warning(
                    "VideoPipeline: cached chapter segmentation is invalid; "
                    "rerunning path=%s",
                    chapter_out_path,
                )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "VideoPipeline: failed to load cached chapter segmentation; "
                "rerunning: %s",
                e,
            )

    if not reused_chapters:
        chapter_json = _segment_chapters(
            asr_segments,
            video_duration_s,
            llm_port=llm_port,
        )

    seg_conf = "unknown"
    if isinstance(chapter_json, dict):
        seg_conf = (
            str(chapter_json.get("segmentation_confidence", "")).strip().lower()
            or "unknown"
        )
    chapters = (
        chapter_json.get("chapters", []) if isinstance(chapter_json, dict) else []
    )
    chapters = _normalize_chapters_start_only(chapters, video_duration_s)
    if chapters:
        _, last_chapter_end_before = _chapter_time_span(
            chapters, len(chapters) - 1, video_duration_s
        )
        _, last_chapter_end_after = _chapter_time_span(
            chapters, len(chapters) - 1, video_duration_s
        )
    else:
        last_chapter_end_before = 0.0
        last_chapter_end_after = 0.0

    try:
        with open(chapter_out_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "segmentation_confidence": seg_conf,
                    "chapters": chapters,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        logger.debug(
            "VideoPipeline: saved chapter segmentation path=%s", chapter_out_path
        )
    except OSError as save_err:
        logger.warning(
            "VideoPipeline: failed to save temporary chapter segmentation: %s",
            save_err,
        )

    boundaries = _collect_asr_boundaries(asr_segments)
    chapter_boundaries = _collect_chapter_boundaries(chapters)
    last_asr_ts = max(boundaries) if boundaries else 0.0
    max_ch_dur = 0.0
    if chapters:
        for i, _ in enumerate(chapters):
            st, ed = _chapter_time_span(chapters, i, video_duration_s)
            max_ch_dur = max(max_ch_dur, ed - st)
    logger.debug(
        "VideoPipeline: duration=%.3fs last_asr=%.3fs "
        "last_chapter_before=%.3fs last_chapter_after=%.3fs max_chapter=%.3fs",
        video_duration_s,
        last_asr_ts,
        last_chapter_end_before,
        last_chapter_end_after,
        max_ch_dur,
    )
    logger.info(
        "VideoPipeline: chapters=%d confidence=%s asr_text_length=%d",
        len(chapters),
        seg_conf,
        sum(len(str(seg.get("text", ""))) for seg in asr_segments),
    )
    fallback_needed, fallback_reason = _should_fallback_to_legacy(
        asr_segments, asr_segments, chapters, video_duration_s, seg_conf
    )
    logger.debug(
        "VideoPipeline: fixed-segment fallback=%s reason=%s",
        fallback_needed,
        fallback_reason,
    )
    if fallback_needed:
        logger.info(
            "VideoPipeline: using unified ASR-aligned fallback: %s",
            fallback_reason,
        )

    logger.info(
        "VideoPipeline: using LLM plugin=%s VLM plugin=%s",
        type(llm_port).__name__,
        type(vlm_port).__name__,
    )

    def _update_event_table_for_run(
        pre_et: dict[str, Any] | None,
        stm: ShortTermMemory,
        force_continue: bool = False,
    ) -> dict[str, Any]:
        return _update_event_table(
            pre_et,
            stm,
            force_continue=force_continue,
            llm_port=llm_port,
        )

    short_terms: list[ShortTermMemory] = []
    stms_by_id: dict[UUID, ShortTermMemory] = {}
    medium_terms: list[MediumTermMemory] = []
    event_tables: list[dict[str, Any]] = []

    session_clip_ids: list[UUID] = []
    session_details: list[str] = []
    prev_stm: ShortTermMemory | None = None
    current_event_table: dict[str, Any] | None = None
    current_event_id: UUID | None = None
    last_accepted_split_time_s = -1.0
    min_split_gap_s = float(chunk_seconds)
    pending_active = False
    pending_anchor_info: dict[str, Any] | None = None
    pending_stms: list[ShortTermMemory] = []
    pending_event_table: dict[str, Any] | None = None

    def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not path.exists() or not path.is_file():
            return rows
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
        return rows

    def _safe_uuid(raw: Any) -> UUID | None:
        try:
            return raw if isinstance(raw, UUID) else UUID(str(raw))
        except (AttributeError, TypeError, ValueError):
            return None

    def _stm_from_stream_row(d: dict[str, Any]) -> ShortTermMemory | None:
        try:
            sid = _safe_uuid(d.get("id"))
            tr = d.get("time_range", [0.0, 0.0])
            if sid is None or not isinstance(tr, (list, tuple)) or len(tr) != 2:
                return None
            st = float(tr[0])
            ed = float(tr[1])
            if ed < st:
                return None
            visual_summary = str(d.get("visual_summary", ""))
            detailed_caption = str(d.get("detailed_caption", ""))
            asr_text = str(d.get("ASR", ""))
            emb_raw = d.get("embedding", [])
            emb = emb_raw if isinstance(emb_raw, list) else []
            emb_clean = [float(x) for x in emb if isinstance(x, (int, float))]
            return ShortTermMemory(
                id=sid,
                video_source_path=str(d.get("video_source_path", video_path)),
                time_range=(st, ed),
                visual_summary=visual_summary,
                detailed_caption=detailed_caption,
                embedding=emb_clean,
                asr=asr_text,
                environment=str(d.get("environment", "")),
                inferred_intent=str(d.get("inferred_intent", "")),
            )
        except (AttributeError, TypeError, ValueError):
            return None

    def _mtm_from_stream_row(d: dict[str, Any]) -> MediumTermMemory | None:
        try:
            tid = _safe_uuid(d.get("task_id"))
            sp = d.get("time_span", [0.0, 0.0])
            if tid is None or not isinstance(sp, (list, tuple)) or len(sp) != 2:
                return None
            st = float(sp[0])
            ed = float(sp[1])
            if ed < st:
                return None
            child_raw = d.get("child_clip_ids", [])
            child_ids: list[UUID] = []
            if isinstance(child_raw, list):
                for x in child_raw:
                    uid = _safe_uuid(x)
                    if uid is not None:
                        child_ids.append(uid)
            topic = str(d.get("topic", "") or "event")
            narrative_summary = str(d.get("narrative_summary", ""))
            semantic_inference = str(d.get("semantic_inference", ""))
            emb_raw = d.get("embedding", [])
            emb = emb_raw if isinstance(emb_raw, list) else []
            emb_clean = [float(x) for x in emb if isinstance(x, (int, float))]
            return MediumTermMemory(
                task_id=tid,
                topic=topic,
                time_span=(st, ed),
                narrative_summary=narrative_summary,
                semantic_inference=semantic_inference,
                child_clip_ids=child_ids,
                embedding=emb_clean,
            )
        except (AttributeError, TypeError, ValueError):
            return None

    def _record_stm(stm: ShortTermMemory) -> None:
        short_terms.append(stm)
        stms_by_id[stm.id] = stm
        _append_jsonl(st_stream_path, stm.to_dict())

    def _mtm_detail_from_stm(stm: ShortTermMemory) -> str:
        visual = str(stm.detailed_caption or "").strip()
        asr_text = str(stm.asr or "").strip()
        environment = str(stm.environment or "").strip()
        env_line = f"\n[ENV] {environment}" if environment else ""
        if asr_text and asr_text != "无可用音频或转录失败":
            return f"[VISUAL] {visual}{env_line}\n[ASR] {asr_text}".strip()
        return f"[VISUAL] {visual}{env_line}".strip()

    def _finalize_session() -> None:
        if not session_clip_ids:
            return
        first_stm = stms_by_id[session_clip_ids[0]]
        last_stm = stms_by_id[session_clip_ids[-1]]
        topic = first_stm.visual_summary or "事件"
        time_span = (first_stm.time_range[0], last_stm.time_range[1])
        summary = _summarize_session(
            session_details,
            llm_port=llm_port,
        )
        topic = summary.get("topic_label") or topic
        narrative = summary.get("full_narrative") or "\n".join(session_details)
        semantic_inference = summary.get("semantic_inference", "")
        mtm = MediumTermMemory(
            task_id=uuid4(),
            topic=topic,
            time_span=time_span,
            narrative_summary=narrative,
            semantic_inference=semantic_inference,
            child_clip_ids=session_clip_ids,
            embedding=[],
        )
        medium_terms.append(mtm)
        _append_jsonl(mt_stream_path, mtm.to_dict())

    def _chapters_for_segment(start_s: float, end_s: float) -> list[dict[str, Any]]:
        if not chapters:
            return []
        overlapping: list[dict[str, Any]] = []
        for i, ch in enumerate(chapters):
            st, ed = _chapter_time_span(chapters, i, video_duration_s)
            if ed >= start_s and st <= end_s:
                enriched = dict(ch)
                enriched["start_time"] = _seconds_to_hhmmss(st)
                enriched["end_time"] = _seconds_to_hhmmss(ed)
                enriched["time_range"] = [
                    _seconds_to_hhmmss(st),
                    _seconds_to_hhmmss(ed),
                ]
                overlapping.append(enriched)
        return overlapping

    def _chapter_shift_text(boundary_t: float) -> str:
        if not chapters:
            return "ASR shift:unknown -> unknown | evidence=asr."
        eps = 1e-3
        next_idx = -1
        for i in range(1, len(chapters)):
            st = _hhmmss_to_seconds(str(chapters[i].get("start_time", "00:00:00")))
            if abs(st - boundary_t) <= 1.0 + eps:
                next_idx = i
                break
        if next_idx <= 0:
            for i in range(1, len(chapters)):
                st = _hhmmss_to_seconds(str(chapters[i].get("start_time", "00:00:00")))
                if st >= boundary_t - eps:
                    next_idx = i
                    break
        if next_idx <= 0:
            return "ASR shift:unknown -> unknown | evidence=asr."
        prev_ch = chapters[next_idx - 1]
        next_ch = chapters[next_idx]
        prev_summary = (
            str(prev_ch.get("summary") or prev_ch.get("title") or "unknown").strip()
            or "unknown"
        )
        next_summary = (
            str(next_ch.get("summary") or next_ch.get("title") or "unknown").strip()
            or "unknown"
        )
        return f"ASR shift:{prev_summary}->{next_summary} | evidence=asr."

    def _fixed_length_segments() -> list[dict[str, float]]:
        step = max(1.0, float(chunk_seconds))
        segments: list[dict[str, float]] = []
        start_t = 0.0
        while start_t < video_duration_s - 1e-6:
            end_t = min(video_duration_s, start_t + step)
            segments.append({"start_s": start_t, "end_s": end_t})
            start_t = end_t
        if not segments and video_duration_s > 0.0:
            segments.append({"start_s": 0.0, "end_s": video_duration_s})
        for idx, seg in enumerate(segments, start=1):
            seg["segment_id"] = idx
        return segments

    def _prepare_segments() -> list[dict[str, float]]:
        asr_text_len = sum(
            len(str(seg.get("text", "")).strip()) for seg in asr_segments
        )
        if asr_text_len <= 0:
            logger.info(
                "VideoPipeline: no usable ASR text; using fixed-length segments"
            )
            return _fixed_length_segments()

        segs = make_asr_aligned_segments(
            0.0,
            video_duration_s,
            boundaries,
            SegmentPolicy(
                target_len_s=float(chunk_seconds),
                min_len_s=max(1.0, float(chunk_seconds) - 5.0),
                max_len_s=float(chunk_seconds) + 5.0,
                snap_window_s=10.0,
            ),
        )
        segs = _merge_short_segments(
            segs, min_len_s=5.0, chapter_start_s=0.0, chapter_end_s=video_duration_s
        )
        merged: list[dict[str, float]] = []
        for seg in segs:
            st = float(seg.get("start_s", 0.0))
            ed = float(seg.get("end_s", st))
            asr_lines = _asr_for_range(asr_segments, st, ed)
            if not asr_lines and merged:
                merged[-1]["end_s"] = max(merged[-1]["end_s"], ed)
            else:
                merged.append({"start_s": st, "end_s": ed})
        for idx, seg in enumerate(merged, start=1):
            seg["segment_id"] = idx
        return merged

    def _is_segment_already_covered(start_t: float, end_t: float) -> bool:
        """Check whether existing STMs already cover this base segment interval."""
        if not short_terms:
            return False
        eps = 1e-3
        intervals: list[tuple[float, float]] = []
        for stm in short_terms:
            st = float(stm.time_range[0])
            ed = float(stm.time_range[1])
            if ed <= start_t + eps or st >= end_t - eps:
                continue
            cs = max(start_t, st)
            ce = min(end_t, ed)
            if ce - cs > eps:
                intervals.append((cs, ce))

        if not intervals:
            return False

        intervals.sort(key=lambda x: x[0])
        merged: list[tuple[float, float]] = []
        for st, ed in intervals:
            if not merged or st > merged[-1][1] + 0.05:
                merged.append((st, ed))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], ed))

        cursor = start_t
        for st, ed in merged:
            if st > cursor + 0.25:
                return False
            cursor = max(cursor, ed)
            if cursor >= end_t - 0.25:
                return True
        return cursor >= end_t - 0.25

    if resume_active:
        restored_st = 0
        restored_mt = 0
        restored_et = 0

        st_rows = _iter_jsonl(st_stream_path)
        parsed_st: list[ShortTermMemory] = []
        seen_st_ids: set[UUID] = set()
        for item in st_rows:
            stm = _stm_from_stream_row(item)
            if stm is None or stm.id in seen_st_ids:
                continue
            parsed_st.append(stm)
            seen_st_ids.add(stm.id)

        # If a crashed/buggy rerun appended fresh clips from t~0 at tail,
        # keep only the monotonic historical prefix and drop the rollback suffix.
        rollback_idx = -1
        prev_end = -1.0
        for idx, stm in enumerate(parsed_st):
            st_v = float(stm.time_range[0])
            ed_v = float(stm.time_range[1])
            if idx > 0 and st_v + 1e-3 < prev_end - 1.0:
                rollback_idx = idx
                break
            prev_end = max(prev_end, ed_v)

        rollback_trimmed = rollback_idx >= 0
        if rollback_trimmed:
            logger.warning(
                "VideoPipeline: discarded rollback-appended clip tail from row=%d",
                rollback_idx + 1,
            )
            parsed_st = parsed_st[:rollback_idx]

        for stm in parsed_st:
            short_terms.append(stm)
            stms_by_id[stm.id] = stm
            restored_st += 1

        short_terms.sort(
            key=lambda s: (float(s.time_range[0]), float(s.time_range[1]), str(s.id))
        )

        st_resume_end_s = max(
            (float(s.time_range[1]) for s in short_terms), default=-1.0
        )
        valid_st_ids = set(stms_by_id.keys())

        seen_mtm_ids: set[UUID] = set()
        for item in _iter_jsonl(mt_stream_path):
            mtm = _mtm_from_stream_row(item)
            if mtm is None or mtm.task_id in seen_mtm_ids:
                continue
            if st_resume_end_s >= 0 and float(mtm.time_span[1]) > st_resume_end_s + 1.0:
                continue
            if mtm.child_clip_ids and any(
                cid not in valid_st_ids for cid in mtm.child_clip_ids
            ):
                continue
            medium_terms.append(mtm)
            seen_mtm_ids.add(mtm.task_id)
            restored_mt += 1

        for item in _iter_jsonl(et_stream_path):
            cid = _safe_uuid(item.get("clip_id"))
            if cid is None or cid not in valid_st_ids:
                continue
            tr = item.get("time_range", [])
            if isinstance(tr, (list, tuple)) and len(tr) == 2 and st_resume_end_s >= 0:
                try:
                    if float(tr[1]) > st_resume_end_s + 1.0:
                        continue
                except (TypeError, ValueError):
                    continue
            event_tables.append(item)
            restored_et += 1

        if rollback_trimmed:
            # Rewrite sanitized streams so future resumes are deterministic.
            with open(st_stream_path, "w", encoding="utf-8") as f:
                for s in short_terms:
                    f.write(json.dumps(s.to_dict(), ensure_ascii=True) + "\n")
            with open(mt_stream_path, "w", encoding="utf-8") as f:
                for m in medium_terms:
                    f.write(json.dumps(m.to_dict(), ensure_ascii=True) + "\n")
            with open(et_stream_path, "w", encoding="utf-8") as f:
                for rec in event_tables:
                    f.write(json.dumps(rec, ensure_ascii=True) + "\n")

        # Rebuild active session state from last committed ET event chain.
        if event_tables:
            valid_chain: list[tuple[dict[str, Any], UUID]] = []
            for rec in event_tables:
                cid = _safe_uuid(rec.get("clip_id"))
                if cid is None or cid not in stms_by_id:
                    continue
                valid_chain.append((rec, cid))

            if valid_chain:
                last_rec, _ = valid_chain[-1]
                evt_raw = str(last_rec.get("event_id", "")).strip()
                current_event_id = _safe_uuid(evt_raw)
                committed = last_rec.get("event_table_committed")
                if not isinstance(committed, dict):
                    committed = last_rec.get("event_table")
                current_event_table = committed if isinstance(committed, dict) else None

                if current_event_id is not None:
                    for rec, cid in valid_chain:
                        if str(rec.get("event_id", "")).strip() == str(
                            current_event_id
                        ):
                            session_clip_ids.append(cid)

                session_details = [
                    _mtm_detail_from_stm(stms_by_id[cid])
                    for cid in session_clip_ids
                    if cid in stms_by_id
                ]
                prev_stm = (
                    stms_by_id[session_clip_ids[-1]] if session_clip_ids else None
                )

        for rec in event_tables:
            decision = rec.get("decision")
            if not isinstance(decision, dict):
                continue
            split_t = decision.get("split_t")
            if split_t is None:
                continue
            try:
                split_v = float(split_t)
            except (TypeError, ValueError):
                continue
            if split_v > last_accepted_split_time_s:
                last_accepted_split_time_s = split_v

        logger.info(
            "VideoPipeline: restored clips=%d events=%d event_tables=%d "
            "active_session_clips=%d",
            restored_st,
            restored_mt,
            restored_et,
            len(session_clip_ids),
        )

    def _append_event_table_record(
        *,
        event_id: str,
        clip_id: str,
        time_range: list[float],
        committed_et: dict[str, Any],
        proposed_et: dict[str, Any] | None = None,
        decision: dict[str, Any] | None = None,
    ) -> None:
        """Persist both pre-judge proposal and committed ET for traceability."""
        rec = {
            "event_id": event_id,
            "clip_id": clip_id,
            "time_range": time_range,
            # Backward compatible field consumed by existing scripts.
            "event_table": committed_et,
            "event_table_proposed": proposed_et
            if proposed_et is not None
            else committed_et,
            "event_table_committed": committed_et,
        }
        if decision is not None:
            rec["decision"] = decision
        event_tables.append(rec)
        _append_jsonl(et_stream_path, rec)

    def _is_et_only_cooldown_active(
        start_t: float, candidate_sources: list[str]
    ) -> bool:
        """Suppress ET-only re-splitting immediately after a confirmed cut."""
        if candidate_sources != ["ET"]:
            return False
        if last_accepted_split_time_s < 0:
            return False
        gap = start_t - last_accepted_split_time_s
        return 0.0 <= gap < min_split_gap_s

    def _apply_split(split_t: float, start_t: float, end_t: float, seg_id: int) -> None:
        nonlocal session_clip_ids
        nonlocal session_details
        nonlocal prev_stm
        nonlocal current_event_table
        nonlocal current_event_id
        nonlocal last_accepted_split_time_s

        pre_stm, _, _ = _run_stm_for_segment(start_t, split_t, f"{seg_id}_a")
        if pre_stm:
            _record_stm(pre_stm)
            session_clip_ids.append(pre_stm.id)
            session_details.append(_mtm_detail_from_stm(pre_stm))
            prev_stm = pre_stm
            pre_proposed = _update_event_table_for_run(current_event_table, pre_stm)
            current_event_table = pre_proposed
            _append_event_table_record(
                event_id=str(current_event_id) if current_event_id else "",
                clip_id=str(pre_stm.id),
                time_range=[start_t, split_t],
                proposed_et=pre_proposed,
                committed_et=current_event_table,
                decision={"mode": "split_pre", "split_t": split_t},
            )

        if session_clip_ids:
            _finalize_session()
        session_clip_ids = []
        session_details = []
        prev_stm = None
        current_event_table = None
        current_event_id = None
        last_accepted_split_time_s = split_t

        post_stm, _, _ = _run_stm_for_segment(split_t, end_t, f"{seg_id}_b")
        if post_stm:
            _record_stm(post_stm)
            session_clip_ids = [post_stm.id]
            session_details = [_mtm_detail_from_stm(post_stm)]
            prev_stm = post_stm
            current_event_id = uuid4()
            post_proposed = _update_event_table_for_run(None, post_stm)
            current_event_table = post_proposed
            _append_event_table_record(
                event_id=str(current_event_id),
                clip_id=str(post_stm.id),
                time_range=[split_t, end_t],
                proposed_et=post_proposed,
                committed_et=current_event_table,
                decision={"mode": "split_post", "split_t": split_t},
            )

    def _parse_split_seconds(raw_t: str) -> float | None:
        t = str(raw_t or "").strip()
        if not t:
            return None
        try:
            return float(t)
        except (TypeError, ValueError):
            pass
        try:
            return _hhmmss_to_seconds(t)
        except (AttributeError, TypeError, ValueError):
            return None

    def _pick_split_t(
        split_points: list[dict[str, str]],
        start_t: float,
        end_t: float,
        fallback_t: float | None = None,
    ) -> float | None:
        eps = 1e-3
        for sp in split_points:
            t_s = _parse_split_seconds(sp.get("t", ""))
            if t_s is None:
                continue
            # Allow split at the left edge (new clip starts a new event),
            # but keep right edge exclusive to avoid empty post-split segment.
            if start_t - eps <= t_s < end_t - eps:
                return t_s
        if fallback_t is not None and start_t - eps <= fallback_t < end_t - eps:
            return float(fallback_t)
        return None

    def _run_stm_for_segment(
        start_t: float, end_t: float, seg_tag: str
    ) -> tuple[ShortTermMemory | None, list[float], str]:
        out_clip = segments_dir / f"seg_{seg_tag}.mp4"
        logger.debug(
            "VideoPipeline: extracting segment=%s start=%.3f end=%.3f",
            seg_tag,
            start_t,
            end_t,
        )
        try:
            _extract_video_subclip(video_path, out_clip, start_t, end_t)
        except Exception as e:
            logger.exception(
                "VideoPipeline: failed to extract segment=%s start=%.3f end=%.3f",
                seg_tag,
                start_t,
                end_t,
            )
            raise RuntimeError(f"failed to extract video segment {seg_tag}") from e
        logger.debug("VideoPipeline: segment=%s ready path=%s", seg_tag, out_clip)

        asr_lines = _asr_for_range(asr_segments, start_t, end_t)
        asr_text = "\n".join(asr_lines) if asr_lines else "无可用音频或转录失败"
        pre_et_text = (
            "null"
            if not current_event_table
            else json.dumps(current_event_table, ensure_ascii=False, indent=2)
        )
        prompt = CAPTION_PROMPT.replace("{event_table}", pre_et_text)
        logger.debug("VideoPipeline: running VLM inference segment=%s", seg_tag)
        cap = _offline_vu(
            out_clip,
            prompt,
            llm_port=vlm_port,
        )
        logger.debug("VideoPipeline: VLM inference completed segment=%s", seg_tag)

        visual_summary = str(cap.get("visual_summary", "")).strip()
        detailed_caption = str(cap.get("detailed_caption", "")).strip()
        environment = str(cap.get("environment", "")).strip()
        asr_corrected = asr_text

        stm = ShortTermMemory(
            id=uuid4(),
            video_source_path=str(video_path),
            time_range=(start_t, end_t),
            visual_summary=visual_summary,
            detailed_caption=detailed_caption,
            embedding=[],
            asr=asr_corrected,
            environment=environment,
        )
        return stm, [], asr_text

    def _rebuild_event_table_from_old(
        old_et: dict[str, Any] | None,
        pending_items: list[ShortTermMemory],
    ) -> dict[str, Any]:
        rebuilt = old_et
        for item in pending_items:
            rebuilt = _update_event_table_for_run(
                rebuilt,
                item,
                force_continue=True,
            )
        if isinstance(rebuilt, dict):
            return rebuilt
        if pending_items:
            return _update_event_table_for_run(
                old_et,
                pending_items[-1],
                force_continue=True,
            )
        return old_et or {}

    try:
        segments = _prepare_segments()
        logger.info("VideoPipeline: processing segments=%d", len(segments))
        for seg in segments:
            start_t = round(float(seg["start_s"]), 3)
            end_t = round(float(seg["end_s"]), 3)
            if resume_active and _is_segment_already_covered(start_t, end_t):
                logger.debug(
                    "VideoPipeline: skipping restored segment=%d start=%.3f end=%.3f",
                    int(seg.get("segment_id", 0) or 0),
                    start_t,
                    end_t,
                )
                continue
            segment_chapter_boundaries = [
                b for b in chapter_boundaries if start_t <= b < end_t
            ]

            stm_full, _, _ = _run_stm_for_segment(
                start_t, end_t, f"{seg['segment_id']}"
            )
            if stm_full is None:
                continue

            if current_event_table is None:
                if segment_chapter_boundaries:
                    boundary_time = min(segment_chapter_boundaries)
                    _append_jsonl(
                        event_link_log_path,
                        {
                            "ts": datetime.utcnow().isoformat() + "Z",
                            "meta": {
                                "segment_id": int(seg.get("segment_id", 0) or 0),
                                "time_range": [start_t, end_t],
                                "boundary_source": "chapter_segment",
                                "boundary_time": boundary_time,
                                "boundary_time_hhmmss": _seconds_to_hhmmss(
                                    boundary_time
                                ),
                                "lookahead_clips": 1,
                                "init_pre_et": True,
                                "init_action": "direct_split",
                            },
                            "candidate_source": _chapter_shift_text(boundary_time),
                            "pre_ET": None,
                            "current_segments": _event_link_current_segments_payload(
                                stm_full, stm_full
                            ),
                            "note": "Chapter boundary observed during initialization; split applied immediately.",
                        },
                    )
                    _apply_split(
                        boundary_time,
                        start_t,
                        end_t,
                        int(seg.get("segment_id", 0) or 0),
                    )
                    continue

                _record_stm(stm_full)
                session_clip_ids = [stm_full.id]
                session_details = [_mtm_detail_from_stm(stm_full)]
                prev_stm = stm_full
                current_event_id = uuid4()
                init_proposed = _update_event_table_for_run(None, stm_full)
                current_event_table = init_proposed
                _append_event_table_record(
                    event_id=str(current_event_id),
                    clip_id=str(stm_full.id),
                    time_range=[start_t, end_t],
                    proposed_et=init_proposed,
                    committed_et=current_event_table,
                    decision={"mode": "init_from_null_pre_et"},
                )
                continue

            if pending_active and pending_anchor_info is not None:
                pending_stms.append(stm_full)
                pending_event_table = _update_event_table_for_run(
                    pending_event_table,
                    stm_full,
                )

                left_event_table = pending_anchor_info.get("left_event_table")
                left_event_id = pending_anchor_info.get("left_event_id")
                left_session_ids = list(
                    pending_anchor_info.get("left_session_clip_ids") or []
                )
                left_session_detail_list = list(
                    pending_anchor_info.get("left_session_details") or []
                )

                anchor_stm = pending_stms[0]
                pending_confirm_stm = pending_stms[-1]
                pending_start_t = float(pending_stms[0].time_range[0])
                pending_end_t = float(pending_stms[-1].time_range[1])
                pending_chapters = _chapters_for_segment(pending_start_t, pending_end_t)

                effective_candidate_sources = list(
                    pending_anchor_info.get("candidate_sources") or []
                )
                anchor_end_t = float(
                    pending_anchor_info.get("anchor_end_t", pending_start_t)
                )
                pending_new_chapter_boundaries = [
                    b for b in chapter_boundaries if anchor_end_t <= b < pending_end_t
                ]
                if (
                    pending_new_chapter_boundaries
                    and "chapter_segment" not in effective_candidate_sources
                ):
                    effective_candidate_sources.append("chapter_segment")

                has_asr_source = "chapter_segment" in effective_candidate_sources
                has_et_source = "ET" in effective_candidate_sources
                if has_asr_source and pending_new_chapter_boundaries:
                    pending_boundary_time = min(pending_new_chapter_boundaries)
                else:
                    pending_boundary_time = float(
                        pending_anchor_info.get("boundary_time", pending_start_t)
                    )
                pending_source_tag = (
                    "ET+chapter_segment"
                    if len(effective_candidate_sources) > 1
                    else (
                        effective_candidate_sources[0]
                        if effective_candidate_sources
                        else "unknown"
                    )
                )
                parts: list[str] = []
                if has_asr_source:
                    parts.append(_chapter_shift_text(pending_boundary_time))
                if has_et_source:
                    et_evidence = "both" if has_asr_source else "et"
                    parts.append(
                        _format_et_shift_candidate(
                            str(pending_anchor_info.get("trigger_delta", "")),
                            et_evidence,
                        )
                    )
                pending_candidate_source_text = (
                    " | ".join(parts)
                    if parts
                    else str(
                        pending_anchor_info.get("candidate_source_text", "unknown")
                    )
                )
                pending_anchor_info["candidate_sources"] = effective_candidate_sources
                pending_anchor_info["candidate_source_text"] = (
                    pending_candidate_source_text
                )
                pending_anchor_info["boundary_source"] = pending_source_tag
                pending_anchor_info["boundary_time"] = pending_boundary_time

                same_event, split_points = _judge_event_with_et(
                    EventLinkRequest(
                        pre_event=left_event_table,
                        anchor=anchor_stm,
                        pending=pending_confirm_stm,
                        segmentation_confidence=seg_conf,
                        chapters=pending_chapters,
                        candidate_source=pending_candidate_source_text,
                    ),
                    llm_port=llm_port,
                    log_path=event_link_log_path,
                    meta={
                        "segment_id": int(seg.get("segment_id", 0) or 0),
                        "time_range": [pending_start_t, pending_end_t],
                        "boundary_source": "pending_delayed_gate",
                        "pending_candidate_source": pending_source_tag,
                        "boundary_time": pending_boundary_time,
                        "boundary_time_hhmmss": _seconds_to_hhmmss(
                            pending_boundary_time
                        ),
                        "anchor_segment_id": pending_anchor_info.get(
                            "anchor_segment_id"
                        ),
                        "anchor_time_range": [
                            pending_anchor_info.get("anchor_start_t", pending_start_t),
                            pending_anchor_info.get("anchor_end_t", pending_end_t),
                        ],
                        "trigger_delta": pending_anchor_info.get("trigger_delta", ""),
                    },
                )
                logger.debug(
                    "VideoPipeline: boundary confirmation anchor=%s segment=%s "
                    "same_event=%s",
                    pending_anchor_info.get("anchor_segment_id"),
                    seg.get("segment_id"),
                    same_event,
                )

                if not same_event:
                    split_t = _pick_split_t(
                        split_points,
                        pending_start_t,
                        pending_end_t,
                        fallback_t=float(
                            pending_anchor_info.get("boundary_time", pending_start_t)
                        ),
                    )
                    split_idx = -1
                    split_boundary_idx = -1
                    split_seg_start = 0.0
                    split_seg_end = 0.0
                    if split_t is not None:
                        eps = 1e-3
                        for i, pstm in enumerate(pending_stms):
                            st_i = float(pstm.time_range[0])
                            ed_i = float(pstm.time_range[1])
                            if st_i + eps < split_t < ed_i - eps:
                                split_idx = i
                                split_seg_start = st_i
                                split_seg_end = ed_i
                                break
                            # split point lands exactly on clip boundary: split by clip index
                            if abs(ed_i - split_t) <= eps:
                                split_boundary_idx = i + 1
                                break
                            if abs(st_i - split_t) <= eps:
                                split_boundary_idx = i
                                break

                    session_clip_ids = left_session_ids
                    session_details = left_session_detail_list
                    prev_stm = (
                        stms_by_id[session_clip_ids[-1]]
                        if session_clip_ids and session_clip_ids[-1] in stms_by_id
                        else None
                    )
                    current_event_table = (
                        left_event_table
                        if isinstance(left_event_table, dict)
                        else current_event_table
                    )
                    current_event_id = (
                        left_event_id
                        if isinstance(left_event_id, UUID)
                        else current_event_id
                    )

                    if split_idx >= 0 and split_t is not None:
                        for pstm in pending_stms[:split_idx]:
                            _record_stm(pstm)
                            session_clip_ids.append(pstm.id)
                            session_details.append(_mtm_detail_from_stm(pstm))
                            prev_stm = pstm
                            current_event_table = _update_event_table_for_run(
                                current_event_table, pstm, force_continue=True
                            )
                            _append_event_table_record(
                                event_id=str(current_event_id)
                                if current_event_id
                                else "",
                                clip_id=str(pstm.id),
                                time_range=[pstm.time_range[0], pstm.time_range[1]],
                                proposed_et=pending_event_table,
                                committed_et=current_event_table,
                                decision={
                                    "mode": "pending_accept_pre_split",
                                    "split_t": split_t,
                                    "pending_index": split_idx,
                                    "candidate_sources": pending_anchor_info.get(
                                        "candidate_sources"
                                    )
                                    or [],
                                },
                            )

                        _apply_split(
                            split_t,
                            split_seg_start,
                            split_seg_end,
                            int(seg.get("segment_id", 0) or 0),
                        )

                        for pstm in pending_stms[split_idx + 1:]:
                            _record_stm(pstm)
                            session_clip_ids.append(pstm.id)
                            session_details.append(_mtm_detail_from_stm(pstm))
                            prev_stm = pstm
                            proposed_after = _update_event_table_for_run(
                                current_event_table, pstm, force_continue=True
                            )
                            current_event_table = proposed_after
                            _append_event_table_record(
                                event_id=str(current_event_id)
                                if current_event_id
                                else "",
                                clip_id=str(pstm.id),
                                time_range=[pstm.time_range[0], pstm.time_range[1]],
                                proposed_et=proposed_after,
                                committed_et=current_event_table,
                                decision={
                                    "mode": "split_post_attach",
                                    "split_t": split_t,
                                    "candidate_sources": pending_anchor_info.get(
                                        "candidate_sources"
                                    )
                                    or [],
                                },
                            )
                    elif split_boundary_idx >= 0 and split_t is not None:
                        # Boundary split: no need to re-run clip inference; split by existing clip boundaries.
                        for pstm in pending_stms[:split_boundary_idx]:
                            _record_stm(pstm)
                            session_clip_ids.append(pstm.id)
                            session_details.append(_mtm_detail_from_stm(pstm))
                            prev_stm = pstm
                            current_event_table = _update_event_table_for_run(
                                current_event_table, pstm, force_continue=True
                            )
                            _append_event_table_record(
                                event_id=str(current_event_id)
                                if current_event_id
                                else "",
                                clip_id=str(pstm.id),
                                time_range=[pstm.time_range[0], pstm.time_range[1]],
                                proposed_et=pending_event_table,
                                committed_et=current_event_table,
                                decision={
                                    "mode": "pending_accept_pre_boundary",
                                    "split_t": split_t,
                                    "split_boundary_idx": split_boundary_idx,
                                    "candidate_sources": pending_anchor_info.get(
                                        "candidate_sources"
                                    )
                                    or [],
                                },
                            )

                        if session_clip_ids:
                            _finalize_session()

                        current_event_id = uuid4()
                        current_event_table = None
                        session_clip_ids = []
                        session_details = []
                        prev_stm = None

                        for pstm in pending_stms[split_boundary_idx:]:
                            _record_stm(pstm)
                            session_clip_ids.append(pstm.id)
                            session_details.append(_mtm_detail_from_stm(pstm))
                            prev_stm = pstm
                            proposed_after = _update_event_table_for_run(
                                current_event_table, pstm
                            )
                            current_event_table = proposed_after
                            _append_event_table_record(
                                event_id=str(current_event_id),
                                clip_id=str(pstm.id),
                                time_range=[pstm.time_range[0], pstm.time_range[1]],
                                proposed_et=proposed_after,
                                committed_et=current_event_table,
                                decision={
                                    "mode": "pending_accept_post_boundary",
                                    "split_t": split_t,
                                    "split_boundary_idx": split_boundary_idx,
                                    "candidate_sources": pending_anchor_info.get(
                                        "candidate_sources"
                                    )
                                    or [],
                                },
                            )
                    else:
                        if session_clip_ids:
                            _finalize_session()

                        current_event_id = uuid4()
                        current_event_table = (
                            pending_event_table
                            if isinstance(pending_event_table, dict)
                            else _update_event_table_for_run(None, pending_stms[0])
                        )
                        session_clip_ids = []
                        session_details = []
                        for idx, pstm in enumerate(pending_stms, start=1):
                            _record_stm(pstm)
                            session_clip_ids.append(pstm.id)
                            session_details.append(_mtm_detail_from_stm(pstm))
                            _append_event_table_record(
                                event_id=str(current_event_id),
                                clip_id=str(pstm.id),
                                time_range=[pstm.time_range[0], pstm.time_range[1]],
                                proposed_et=current_event_table,
                                committed_et=current_event_table,
                                decision={
                                    "mode": "pending_accept_new_event",
                                    "pending_index": idx,
                                    "pending_size": len(pending_stms),
                                    "candidate_sources": pending_anchor_info.get(
                                        "candidate_sources"
                                    )
                                    or [],
                                },
                            )
                        prev_stm = pending_stms[-1] if pending_stms else None
                    last_accepted_split_time_s = float(
                        split_t
                        if split_t is not None
                        else pending_anchor_info.get("anchor_start_t", pending_start_t)
                    )
                else:
                    running_old_et = left_event_table
                    for idx, pstm in enumerate(pending_stms, start=1):
                        _record_stm(pstm)
                        running_old_et = _update_event_table_for_run(
                            running_old_et, pstm, force_continue=True
                        )
                        _append_event_table_record(
                            event_id=str(left_event_id) if left_event_id else "",
                            clip_id=str(pstm.id),
                            time_range=[pstm.time_range[0], pstm.time_range[1]],
                            proposed_et=pending_event_table,
                            committed_et=running_old_et,
                            decision={
                                "mode": "pending_reject_force_continue",
                                "pending_index": idx,
                                "pending_size": len(pending_stms),
                                "candidate_sources": pending_anchor_info.get(
                                    "candidate_sources"
                                )
                                or [],
                            },
                        )
                    current_event_table = running_old_et
                    current_event_id = (
                        left_event_id
                        if isinstance(left_event_id, UUID)
                        else current_event_id
                    )
                    session_clip_ids = left_session_ids + [s.id for s in pending_stms]
                    session_details = left_session_detail_list + [
                        _mtm_detail_from_stm(s) for s in pending_stms
                    ]
                    prev_stm = pending_stms[-1]

                pending_active = False
                pending_anchor_info = None
                pending_stms = []
                pending_event_table = None
                continue

            proposed_event_table = _update_event_table_for_run(
                current_event_table,
                stm_full,
            )
            delta = str(proposed_event_table.get("delta", "")).strip()
            delta_upper = delta.upper()
            is_candidate = (
                current_event_table is not None
                and delta_upper.startswith("SHIFT:")
                and "NONE ->" not in delta_upper
                and not _is_initialization_shift(delta)
            )
            candidate_sources: list[str] = []
            if is_candidate:
                candidate_sources.append("ET")
            if segment_chapter_boundaries:
                candidate_sources.append("chapter_segment")

            et_only_cooldown_active = _is_et_only_cooldown_active(
                start_t, candidate_sources
            )

            if et_only_cooldown_active:
                _record_stm(stm_full)
                session_clip_ids.append(stm_full.id)
                session_details.append(_mtm_detail_from_stm(stm_full))
                prev_stm = stm_full
                current_event_table = _update_event_table_for_run(
                    current_event_table, stm_full, force_continue=True
                )
                _append_event_table_record(
                    event_id=str(current_event_id) if current_event_id else "",
                    clip_id=str(stm_full.id),
                    time_range=[start_t, end_t],
                    proposed_et=proposed_event_table,
                    committed_et=current_event_table,
                    decision={
                        "mode": "et_only_cooldown_suppressed",
                        "candidate_sources": candidate_sources,
                        "last_accepted_split_time_s": last_accepted_split_time_s,
                        "cooldown_gap_s": start_t - last_accepted_split_time_s,
                        "cooldown_window_s": min_split_gap_s,
                    },
                )
                _append_jsonl(
                    event_link_log_path,
                    {
                        "ts": datetime.utcnow().isoformat() + "Z",
                        "meta": {
                            "segment_id": int(seg.get("segment_id", 0) or 0),
                            "time_range": [start_t, end_t],
                            "delta": delta,
                            "boundary_source": "ET",
                            "boundary_time": start_t,
                            "boundary_time_hhmmss": _seconds_to_hhmmss(start_t),
                            "last_accepted_split_time_s": last_accepted_split_time_s,
                            "cooldown_gap_s": start_t - last_accepted_split_time_s,
                            "cooldown_window_s": min_split_gap_s,
                        },
                        "candidate_source": _format_et_shift_candidate(delta, "et"),
                        "pre_ET": _event_link_pre_et_payload(current_event_table),
                        "current_segments": _event_link_current_segments_payload(
                            stm_full, stm_full
                        ),
                        "proposed_event_table": proposed_event_table,
                        "committed_event_table": current_event_table,
                        "note": "ET-only candidate suppressed by nearby cooldown after a recent confirmed cut.",
                    },
                )
                continue

            if candidate_sources and current_event_table is not None:
                boundary_time = (
                    min(segment_chapter_boundaries)
                    if segment_chapter_boundaries
                    else start_t
                )
                source_tag = (
                    "ET+chapter_segment"
                    if len(candidate_sources) > 1
                    else candidate_sources[0]
                )
                parts: list[str] = []
                has_asr_source = "chapter_segment" in candidate_sources
                has_et_source = "ET" in candidate_sources
                if has_asr_source:
                    parts.append(_chapter_shift_text(boundary_time))
                if has_et_source:
                    et_evidence = "both" if has_asr_source else "et"
                    parts.append(_format_et_shift_candidate(delta, et_evidence))
                prompt_candidate_source = " | ".join(parts) if parts else "unknown"
                pending_active = True
                pending_anchor_info = {
                    "left_event_table": current_event_table,
                    "left_event_id": current_event_id,
                    "left_session_clip_ids": list(session_clip_ids),
                    "left_session_details": list(session_details),
                    "candidate_sources": list(candidate_sources),
                    "candidate_source_text": prompt_candidate_source,
                    "trigger_delta": delta,
                    "anchor_segment_id": int(seg.get("segment_id", 0) or 0),
                    "anchor_start_t": start_t,
                    "anchor_end_t": end_t,
                    "boundary_source": source_tag,
                    "boundary_time": boundary_time,
                }
                pending_stms = [stm_full]
                pending_event_table = _update_event_table_for_run(None, stm_full)
                _append_jsonl(
                    event_link_log_path,
                    {
                        "ts": datetime.utcnow().isoformat() + "Z",
                        "meta": {
                            "segment_id": int(seg.get("segment_id", 0) or 0),
                            "time_range": [start_t, end_t],
                            "delta": delta,
                            "boundary_source": source_tag,
                            "boundary_time": boundary_time,
                            "boundary_time_hhmmss": _seconds_to_hhmmss(boundary_time),
                            "lookahead_clips": 1,
                        },
                        "candidate_source": prompt_candidate_source,
                        "pre_ET": _event_link_pre_et_payload(current_event_table),
                        "current_segments": _event_link_current_segments_payload(
                            stm_full, stm_full
                        ),
                        "proposed_event_table": proposed_event_table,
                        "pending_event_table": pending_event_table,
                        "note": "Pending opened: delayed confirmation will judge after one right clip.",
                    },
                )
                continue
            else:
                _record_stm(stm_full)
                session_clip_ids.append(stm_full.id)
                session_details.append(_mtm_detail_from_stm(stm_full))
                prev_stm = stm_full
                if _is_initialization_shift(delta):
                    current_event_table = _update_event_table_for_run(
                        current_event_table, stm_full, force_continue=True
                    )
                    decision_mode = "no_judge_force_continue_init_shift"
                else:
                    current_event_table = proposed_event_table
                    decision_mode = "no_judge_direct_commit"
                _append_event_table_record(
                    event_id=str(current_event_id) if current_event_id else "",
                    clip_id=str(stm_full.id),
                    time_range=[start_t, end_t],
                    proposed_et=proposed_event_table,
                    committed_et=current_event_table,
                    decision={"mode": decision_mode},
                )

        if pending_active and pending_anchor_info is not None and pending_stms:
            left_event_table = pending_anchor_info.get("left_event_table")
            left_event_id = pending_anchor_info.get("left_event_id")
            left_session_ids = list(
                pending_anchor_info.get("left_session_clip_ids") or []
            )
            left_session_detail_list = list(
                pending_anchor_info.get("left_session_details") or []
            )
            current_event_table = _rebuild_event_table_from_old(
                left_event_table, pending_stms
            )
            current_event_id = (
                left_event_id if isinstance(left_event_id, UUID) else current_event_id
            )
            for pstm in pending_stms:
                _record_stm(pstm)
            session_clip_ids = left_session_ids + [s.id for s in pending_stms]
            session_details = left_session_detail_list + [
                _mtm_detail_from_stm(s) for s in pending_stms
            ]
            prev_stm = pending_stms[-1]
            for idx, pstm in enumerate(pending_stms, start=1):
                _append_event_table_record(
                    event_id=str(current_event_id) if current_event_id else "",
                    clip_id=str(pstm.id),
                    time_range=[pstm.time_range[0], pstm.time_range[1]],
                    proposed_et=pending_event_table,
                    committed_et=current_event_table,
                    decision={
                        "mode": "pending_flush_reject_no_lookahead",
                        "pending_index": idx,
                        "pending_size": len(pending_stms),
                        "candidate_sources": pending_anchor_info.get(
                            "candidate_sources"
                        )
                        or [],
                    },
                )
            pending_active = False
            pending_anchor_info = None
            pending_stms = []
            pending_event_table = None

        if session_clip_ids:
            _finalize_session()
    finally:
        gc.collect()
        import torch
        import torch.distributed as dist
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        # Ensure any distributed/NCCL groups are torn down to avoid leaks
        try:
            if dist.is_available() and dist.is_initialized():
                dist.destroy_process_group()
        except RuntimeError as e:
            logger.warning("VideoPipeline: failed to destroy process group: %s", e)

    logger.info(
        "VideoPipeline: completed clips=%d events=%d",
        len(short_terms),
        len(medium_terms),
    )
    # -------------------- Cleanup temporary artifacts --------------------
    if cleanup:
        import shutil

        # Remove extracted segment mp4s
        try:
            if segments_dir.exists():
                shutil.rmtree(segments_dir, ignore_errors=True)
        except OSError as e:
            logger.warning("VideoPipeline: failed to remove segments: %s", e)

        # All pipeline files are temporary; memories have already been materialized.
        try:
            if outputs_dir.exists():
                shutil.rmtree(outputs_dir, ignore_errors=True)
        except OSError as e:
            logger.warning("VideoPipeline: failed to remove outputs: %s", e)

        try:
            if asr_tmp_work_dir.exists():
                shutil.rmtree(asr_tmp_work_dir, ignore_errors=True)
        except OSError as e:
            logger.warning("VideoPipeline: failed to remove ASR temporary files: %s", e)

        # Legacy pipeline may create work_root/chunks — remove if present
        try:
            legacy_chunks = Path(work_root) / "chunks"
            if legacy_chunks.exists():
                shutil.rmtree(legacy_chunks, ignore_errors=True)
        except OSError as e:
            logger.warning("VideoPipeline: failed to remove legacy chunks: %s", e)

    return {
        "short_term": [memory.to_dict() for memory in short_terms],
        "medium_term": [memory.to_dict() for memory in medium_terms],
        "event_table": event_tables,
    }
