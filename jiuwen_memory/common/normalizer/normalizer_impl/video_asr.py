"""Remote ASR adapter used by the video normalizer."""

from __future__ import annotations

import json
import shutil
import subprocess
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openai

from jiuwen_memory.common._support import (
    outbound_verify,
    read_outbound_ssl,
    require_ca_file,
    require_https,
)
from jiuwen_memory.common.base import Plugin, PluginType
from jiuwen_memory.common.errors import BackendError, HealthCheckError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class VideoAsrConfig:
    """Settings and output paths for one remote ASR run."""

    service: VideoAsrService
    language: str | None = None
    chunk_seconds: int = 600
    output_json: str | Path | None = None
    cleaned_txt_path: str | Path | None = None
    temp_work_dir: str | Path | None = None
    cleanup: bool = True


class VideoAsrProducer(Factory):
    """Factory for ASR services used by video normalization."""

    TOP_NAME = "asr"


class VideoAsrService(Plugin):
    """Transcribe audio into timestamped ASR segments."""

    @abstractmethod
    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        chunk_seconds: int,
    ) -> list[dict[str, str]]:
        """Return monotonic ``start``/``end``/``text`` segments for one audio file."""


def _executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BackendError(f"required executable is unavailable: {name}")
    return str(Path(path).resolve())


def _ensure_audio(video_path: Path, output_dir: Path) -> Path:
    """Extract 16 kHz mono audio for the remote transcription endpoint."""
    audio_path = output_dir / f"{video_path.stem}.wav"
    subprocess.run(
        [
            _executable("ffmpeg"),
            "-y",
            "-i",
            str(video_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-vn",
            str(audio_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return audio_path


def _get_duration(path: Path) -> float:
    result = subprocess.run(
        [
            _executable("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    try:
        return float(result.stdout.decode().strip())
    except (TypeError, ValueError) as exc:
        raise BackendError(f"failed to read audio duration: {path}") from exc


def _split_audio_to_chunks(
    audio_path: Path,
    out_dir: Path,
    *,
    chunk_seconds: int,
) -> list[tuple[Path, float]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = _get_duration(audio_path)
    if duration <= 0:
        return [(audio_path, 0.0)]

    chunks: list[tuple[Path, float]] = []
    start = 0.0
    index = 0
    while start < duration:
        length = min(float(chunk_seconds), duration - start)
        chunk_path = out_dir / f"audio_chunk_{index:04d}.wav"
        subprocess.run(
            [
                _executable("ffmpeg"),
                "-y",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(audio_path),
                "-t",
                f"{length:.3f}",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-af",
                "asetpts=PTS-STARTPTS",
                str(chunk_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        chunks.append((chunk_path, start))
        start += length
        index += 1
    return chunks


def _seconds_to_hhmmss(seconds: float) -> str:
    total = int(round(max(0.0, seconds)))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _fix_monotonic_timestamps(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fixed: list[dict[str, Any]] = []
    previous_end: float | None = None
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = float(segment.get("start", 0.0))
        end = max(start, float(segment.get("end", start)))
        if previous_end is not None and start < previous_end:
            end += previous_end - start
            start = previous_end
        fixed.append({"start": start, "end": max(start, end), "text": text})
        previous_end = end
    return fixed


def _write_cleaned_segments(segments: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for segment in segments:
            file.write(f"[{segment['start']}-{segment['end']}]: {segment['text']}\n")


def run_video_asr(video_path: str | Path, config: VideoAsrConfig) -> list[dict[str, str]]:
    """Extract audio, send it to the configured remote ASR service, then clean up."""
    source_path = Path(video_path)
    output_json = Path(config.output_json) if config.output_json else None
    cleaned_txt = Path(config.cleaned_txt_path) if config.cleaned_txt_path else None
    temp_dir = (
        Path(config.temp_work_dir)
        if config.temp_work_dir
        else (output_json.parent / "_asr_tmp" if output_json else source_path.parent / ".asr_tmp")
    )
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        audio_path = _ensure_audio(source_path, temp_dir)
        segments = config.service.transcribe(
            audio_path,
            language=config.language,
            chunk_seconds=max(1, config.chunk_seconds),
        )
        if output_json:
            output_json.parent.mkdir(parents=True, exist_ok=True)
            output_json.write_text(
                json.dumps(segments, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if cleaned_txt:
            _write_cleaned_segments(segments, cleaned_txt)
        return segments
    finally:
        if config.cleanup:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except OSError as exc:
                logger.warning("VideoASR: failed to remove temporary files: %s", exc)


class OpenAITranscriptionVideoAsr(VideoAsrService):
    """OpenAI-compatible remote ASR client, including vLLM Whisper servers."""

    def __init__(
        self,
        *,
        model_name: str,
        base_url: str,
        api_key: str = "",
        ssl_verify: bool = False,
        ssl_ca_cert: str | None = None,
        config_source: Any | None = None,
        config_namespace: str = "asr",
    ) -> None:
        self._fallback_model = model_name
        self._fallback_base_url = base_url
        self._fallback_api_key = api_key
        self._ssl_verify = ssl_verify
        self._ssl_ca_cert = ssl_ca_cert
        self._config_source = config_source
        self._config_namespace = config_namespace
        self._client: openai.OpenAI | None = None
        self._client_fingerprint: tuple[str, str | None, bool, str | None] | None = None

    def plugin_type(self) -> PluginType:
        return PluginType.ASR

    def _endpoint(self):
        from jiuwen_memory.config.binding import resolve_endpoint

        return resolve_endpoint(
            self._config_source,
            namespace=self._config_namespace,
            fallback_model=self._fallback_model,
            fallback_api_key=self._fallback_api_key,
            fallback_base_url=self._fallback_base_url,
        )

    @property
    def client(self) -> openai.OpenAI:
        endpoint = self._endpoint()
        fingerprint = (endpoint.api_key, endpoint.base_url, self._ssl_verify, self._ssl_ca_cert)
        if self._client is None or self._client_fingerprint != fingerprint:
            kwargs: dict[str, Any] = {"api_key": endpoint.api_key}
            if endpoint.base_url:
                kwargs["base_url"] = endpoint.base_url
            if self._ssl_verify:
                kwargs["http_client"] = openai.DefaultHttpxClient(
                    verify=outbound_verify(self._ssl_ca_cert)
                )
            self._client = openai.OpenAI(**kwargs)
            self._client_fingerprint = fingerprint
        return self._client

    def health(self) -> None:
        try:
            self.client.models.list()
        except Exception as exc:
            raise HealthCheckError(f"ASR health check failed: {exc}") from exc

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        chunk_seconds: int,
    ) -> list[dict[str, str]]:
        raw_segments: list[dict[str, Any]] = []
        model = self._endpoint().model
        chunks = _split_audio_to_chunks(
            audio_path,
            audio_path.parent / "asr_chunks",
            chunk_seconds=chunk_seconds,
        )
        for chunk_path, offset in chunks:
            request: dict[str, Any] = {
                "model": model,
                "response_format": "verbose_json",
                "timestamp_granularities": ["segment"],
            }
            if language:
                request["language"] = language
            with chunk_path.open("rb") as audio_file:
                response = self.client.audio.transcriptions.create(file=audio_file, **request)
            segments = _transcription_payload(response).get("segments")
            if not isinstance(segments, list):
                raise BackendError(
                    "remote ASR response has no timestamped segments; "
                    "vLLM must support verbose_json segment timestamps"
                )
            for segment in segments:
                if not isinstance(segment, Mapping):
                    continue
                text = str(segment.get("text", "")).strip()
                try:
                    start = float(segment["start"]) + offset
                    end = float(segment["end"]) + offset
                except (KeyError, TypeError, ValueError) as exc:
                    raise BackendError("remote ASR returned an invalid segment timestamp") from exc
                if text and end >= start:
                    raw_segments.append({"start": start, "end": end, "text": text})
        fixed = _fix_monotonic_timestamps(
            sorted(raw_segments, key=lambda segment: float(segment["start"]))
        )
        return [
            {
                "start": _seconds_to_hhmmss(float(segment["start"])),
                "end": _seconds_to_hhmmss(float(segment["end"])),
                "text": str(segment["text"]),
            }
            for segment in fixed
        ]


def _transcription_payload(response: Any) -> dict[str, Any]:
    if isinstance(response, Mapping):
        return dict(response)
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        payload = model_dump()
        if isinstance(payload, dict):
            return payload
    raise BackendError("remote ASR returned an unsupported response type")


@VideoAsrProducer.register("openai_transcription")
def _build_openai_transcription(config: Mapping[str, Any]) -> VideoAsrService:
    base_url = Factory.require_param(config, "asr_base_url", backend="remote ASR")
    ssl = read_outbound_ssl(config, "asr")
    if ssl.verify:
        require_https(base_url, component="remote ASR", param="asr")
        require_ca_file(ssl.ca_cert, component="remote ASR", param="asr")
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    return OpenAITranscriptionVideoAsr(
        model_name=Factory.require_param(config, "asr_model", backend="remote ASR"),
        base_url=base_url,
        api_key=str(config.get("asr_api_key", "")),
        ssl_verify=ssl.verify,
        ssl_ca_cert=ssl.ca_cert,
        config_source=ConfigSourceProducer.get_cached("default"),
        config_namespace=str(config.get("config_namespace", "asr")),
    )
