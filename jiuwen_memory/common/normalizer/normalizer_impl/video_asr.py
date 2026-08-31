"""Remote ASR adapter used by the video normalizer."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
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


def _has_audio_stream(video_path: Path) -> bool:
    """Return whether ffprobe can find a primary audio stream."""
    result = subprocess.run(
        [
            _executable("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(video_path),
        ],
        capture_output=True,
        check=True,
    )
    return bool(result.stdout.strip())


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
        if _has_audio_stream(source_path):
            audio_path = _ensure_audio(source_path, temp_dir)
            segments = config.service.transcribe(
                audio_path,
                language=config.language,
                chunk_seconds=max(1, config.chunk_seconds),
            )
        else:
            logger.warning(
                "VideoASR: video has no audio stream; continuing without ASR path=%s",
                source_path,
            )
            segments = []
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


class DashScopeFileTranscriptionVideoAsr(VideoAsrService):
    """Alibaba Cloud Model Studio asynchronous file transcription adapter."""

    def __init__(
        self,
        *,
        model_name: str,
        base_url: str,
        api_key: str = "",
        poll_interval_seconds: float = 3.0,
        task_timeout_seconds: float = 7200.0,
        ssl_verify: bool = False,
        ssl_ca_cert: str | None = None,
        config_source: Any | None = None,
        config_namespace: str = "asr",
    ) -> None:
        self._fallback_model = model_name
        self._fallback_base_url = base_url
        self._fallback_api_key = api_key
        self._poll_interval_seconds = max(0.1, poll_interval_seconds)
        self._task_timeout_seconds = max(1.0, task_timeout_seconds)
        self._ssl_verify = ssl_verify
        self._ssl_ca_cert = ssl_ca_cert
        self._config_source = config_source
        self._config_namespace = config_namespace

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

    def health(self) -> None:
        endpoint = self._endpoint()
        try:
            _dashscope_api_root(endpoint.base_url)
            _dashscope_oss_utils()
            if not endpoint.api_key:
                raise BackendError("DashScope ASR requires a non-empty API key")
        except (BackendError, ImportError) as exc:
            raise HealthCheckError(f"ASR health check failed: {exc}") from exc

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        chunk_seconds: int,
    ) -> list[dict[str, str]]:
        del chunk_seconds
        endpoint = self._endpoint()
        base_url = _dashscope_api_root(endpoint.base_url)
        if not endpoint.api_key:
            raise BackendError("DashScope ASR requires a non-empty API key")
        headers = {"Authorization": f"Bearer {endpoint.api_key}"}
        audio_url = _upload_dashscope_audio(
            audio_path,
            model=endpoint.model,
            api_key=endpoint.api_key,
            base_url=base_url,
        )
        parameters: dict[str, Any] = {
            "channel_id": [0],
            "enable_itn": True,
            "enable_words": False,
        }
        if language:
            parameters["language"] = language
        submitted = self._request_json(
            "POST",
            f"{base_url}/services/audio/asr/transcription",
            headers={
                **headers,
                "X-DashScope-Async": "enable",
                "X-DashScope-OssResourceResolve": "enable",
            },
            json={
                "model": endpoint.model,
                "input": {"file_url": audio_url},
                "parameters": parameters,
            },
        )
        output = submitted.get("output")
        task_id = output.get("task_id") if isinstance(output, Mapping) else None
        if not isinstance(task_id, str) or not task_id:
            raise BackendError("DashScope ASR submission response has no task_id")

        deadline = time.monotonic() + self._task_timeout_seconds
        while True:
            task = self._request_json(
                "GET",
                f"{base_url}/tasks/{task_id}",
                headers=headers,
            )
            output = task.get("output")
            if not isinstance(output, Mapping):
                raise BackendError("DashScope ASR task response has no output")
            status = str(output.get("task_status", "")).upper()
            if status == "SUCCEEDED":
                result = output.get("result")
                result_url = (
                    result.get("transcription_url") if isinstance(result, Mapping) else None
                )
                if not isinstance(result_url, str) or not result_url:
                    raise BackendError("DashScope ASR task has no transcription_url")
                return _dashscope_segments(self._request_json("GET", result_url))
            if status not in {"PENDING", "RUNNING"}:
                code = output.get("code", task.get("code", "unknown"))
                message = output.get("message", task.get("message", "unknown error"))
                raise BackendError(f"DashScope ASR task {status}: {code}: {message}")
            if time.monotonic() >= deadline:
                raise BackendError(
                    f"DashScope ASR task timed out after {self._task_timeout_seconds:g}s"
                )
            time.sleep(self._poll_interval_seconds)

    def _request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        import requests

        method = method.upper()
        retry_ambiguous_failure = method in {"GET", "HEAD", "OPTIONS"}
        kwargs["timeout"] = 60
        if self._ssl_verify:
            kwargs["verify"] = outbound_verify(self._ssl_ca_cert)
        for attempt in range(3):
            try:
                response = requests.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if not retry_ambiguous_failure or attempt == 2:
                    raise BackendError(
                        f"DashScope ASR request failed: {method} {url}: {exc}"
                    ) from exc
                logger.warning(
                    "DashScope ASR request attempt %d/3 interrupted; retrying %s %s",
                    attempt + 1,
                    method,
                    url,
                )
                time.sleep(2**attempt)
                continue
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                retryable = retry_ambiguous_failure and (
                    response.status_code == 429 or response.status_code >= 500
                )
                if not retryable or attempt == 2:
                    raise BackendError(
                        f"DashScope ASR request failed: {method} {url}: {exc}"
                    ) from exc
                logger.warning(
                    "DashScope ASR request attempt %d/3 returned HTTP %d; "
                    "retrying %s %s",
                    attempt + 1,
                    response.status_code,
                    method,
                    url,
                )
                time.sleep(2**attempt)
                continue
            except requests.RequestException as exc:
                raise BackendError(
                    f"DashScope ASR request failed: {method} {url}: {exc}"
                ) from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise BackendError(
                    f"DashScope ASR returned invalid JSON: {method} {url}: {exc}"
                ) from exc
            break
        if not isinstance(payload, dict):
            raise BackendError("DashScope ASR returned a non-object JSON response")
        return payload


def _dashscope_oss_utils():
    try:
        from dashscope.common.error import UploadFileException
        from dashscope.utils.oss_utils import OssUtils
    except ImportError as exc:
        raise ImportError(
            "DashScope ASR requires `pip install JiuwenMemory[multimodal]`"
        ) from exc
    return OssUtils, UploadFileException


def _upload_dashscope_audio(
    audio_path: Path,
    *,
    model: str,
    api_key: str,
    base_url: str,
) -> str:
    import requests

    try:
        oss_utils, upload_error = _dashscope_oss_utils()
    except ImportError as exc:
        raise BackendError(str(exc)) from exc
    for attempt in range(3):
        try:
            uploaded = oss_utils.upload(
                model=model,
                file_path=str(audio_path),
                api_key=api_key,
                base_address=base_url,
            )
            break
        except (OSError, requests.RequestException, upload_error) as exc:
            if attempt == 2:
                raise BackendError(
                    f"failed to upload audio to DashScope temporary OSS: {exc}"
                ) from exc
            time.sleep(2**attempt)
    audio_url = uploaded[0] if isinstance(uploaded, tuple) else uploaded
    if not isinstance(audio_url, str) or not audio_url.startswith("oss://"):
        raise BackendError("DashScope temporary upload returned an invalid OSS URL")
    return audio_url


def _dashscope_api_root(base_url: str | None) -> str:
    if not base_url:
        raise BackendError("DashScope ASR requires asr_base_url")
    normalized = base_url.rstrip("/")
    suffix = "/services/audio/asr/transcription"
    if normalized.endswith(suffix):
        normalized = normalized[: -len(suffix)]
    if not normalized.endswith("/api/v1"):
        raise BackendError("DashScope ASR base URL must end with /api/v1")
    return normalized


def _dashscope_segments(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    transcripts = payload.get("transcripts")
    if not isinstance(transcripts, list):
        output = payload.get("output")
        transcripts = output.get("transcripts") if isinstance(output, Mapping) else None
    if not isinstance(transcripts, list):
        raise BackendError("DashScope ASR result has no transcripts")

    raw_segments: list[dict[str, Any]] = []
    for transcript in transcripts:
        if not isinstance(transcript, Mapping):
            continue
        sentences = transcript.get("sentences")
        if not isinstance(sentences, list):
            continue
        for sentence in sentences:
            if not isinstance(sentence, Mapping):
                continue
            text = str(sentence.get("text", "")).strip()
            try:
                start = float(sentence["begin_time"]) / 1000.0
                end = float(sentence["end_time"]) / 1000.0
            except (KeyError, TypeError, ValueError) as exc:
                raise BackendError("DashScope ASR returned an invalid sentence timestamp") from exc
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


@VideoAsrProducer.register("dashscope_filetrans")
def _build_dashscope_filetrans(config: Mapping[str, Any]) -> VideoAsrService:
    base_url = Factory.require_param(config, "asr_base_url", backend="DashScope ASR")
    ssl = read_outbound_ssl(config, "asr")
    if ssl.verify:
        require_https(base_url, component="DashScope ASR", param="asr")
        require_ca_file(ssl.ca_cert, component="DashScope ASR", param="asr")
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    return DashScopeFileTranscriptionVideoAsr(
        model_name=Factory.require_param(config, "asr_model", backend="DashScope ASR"),
        base_url=base_url,
        api_key=str(config.get("asr_api_key", "")),
        poll_interval_seconds=float(config.get("asr_poll_interval_seconds", 3.0)),
        task_timeout_seconds=float(config.get("asr_task_timeout_seconds", 7200.0)),
        ssl_verify=ssl.verify,
        ssl_ca_cert=ssl.ca_cert,
        config_source=ConfigSourceProducer.get_cached("default"),
        config_namespace=str(config.get("config_namespace", "asr")),
    )
