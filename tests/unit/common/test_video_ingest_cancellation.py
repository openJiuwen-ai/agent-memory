"""Cancellation stops the next ASR chunk, video model call, poll or application retry."""

# White-box checks exercise internal retry and cancellation propagation boundaries.
# pylint: disable=protected-access

from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
import requests

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.ingest_cancellation import (
    IngestCancelled,
    ingest_cancel_signal,
    wait_ingest_cancelled,
)
from jiuwen_memory.common.normalizer.normalizer_impl import video_asr, video_pipeline
from jiuwen_memory.common.normalizer.normalizer_impl.video_normalizer import VideoNormalizer
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.job_impl.ingest_job import InProcessIngestJobController

pytestmark = pytest.mark.unit


def test_real_ingest_cancel_stops_before_second_asr_chunk(tmp_path, monkeypatch) -> None:
    chunks = [(tmp_path / "one.wav", 0), (tmp_path / "two.wav", 1)]
    for path, _offset in chunks:
        path.write_bytes(b"audio")
    monkeypatch.setattr(video_asr, "_split_audio_to_chunks", lambda *a, **kw: chunks)
    entered, release, finished = Event(), Event(), Event()
    calls = []

    def create(**kwargs):
        calls.append(kwargs["file"].name)
        entered.set()
        assert release.wait(5)
        return {"segments": [{"start": 0, "end": 1, "text": "first"}]}

    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    monkeypatch.setattr(
        video_asr.OpenAITranscriptionVideoAsr, "client", property(lambda self: client)
    )
    asr = video_asr.OpenAITranscriptionVideoAsr(model_name="fake", base_url="http://unused")
    jobs = InProcessIngestJobController()
    original_finish = jobs._finished

    def finish(job_id, future):
        original_finish(job_id, future)
        finished.set()

    monkeypatch.setattr(jobs, "_finished", finish)
    scope = Scope(org="test", user="alice")

    def task():
        asr.transcribe(chunks[0][0], language=None, chunk_seconds=1)
        return []

    try:
        job = jobs.submit(payload_id="p", source_ref="s", scope=scope, task=task).job
        assert entered.wait(2)
        jobs.cancel(job.id, scope=scope)
        assert jobs.status(job.id, scope=scope).status == "running"
        release.set()
        assert finished.wait(3)
        result = jobs.status(job.id, scope=scope)
        assert result.status == "cancelled" and not result.partial_write
        assert len(calls) == 1, "second ASR request must never be sent"
    finally:
        release.set()
        jobs.close()
    # The cancelled task's context must not leak into ordinary synchronous calls.
    asr.transcribe(chunks[0][0], language=None, chunk_seconds=1)
    assert len(calls) == 3


@pytest.mark.parametrize("kind", ["text", "vision"])
@pytest.mark.parametrize("response", ["{}", "invalid json"])
def test_video_model_cancel_is_not_swallowed_or_retried(monkeypatch, kind, response) -> None:
    signal = Event()
    calls = []

    def chat(*args, **kwargs):
        calls.append(1)
        signal.set()
        return response

    monkeypatch.setattr(video_pipeline, "_video_data_url", lambda *a: "data:fake")
    token = ingest_cancel_signal.set(signal)
    try:
        with pytest.raises(IngestCancelled):
            if kind == "text":
                video_pipeline._call_openai_json_with_retry(
                    llm_port=SimpleNamespace(chat=chat), prompt="p", max_tokens=10
                )
            else:
                video_pipeline._offline_vu(Path("fake.mp4"), "p", SimpleNamespace(chat=chat))
        assert calls == [1]
    finally:
        ingest_cancel_signal.reset(token)


def test_dashscope_cancel_stops_request_retry(monkeypatch) -> None:
    signal = Event()
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        signal.set()
        raise requests.ConnectionError("network interrupted")

    monkeypatch.setattr(requests, "request", request)
    asr = video_asr.DashScopeFileTranscriptionVideoAsr(model_name="fake", base_url="unused")
    token = ingest_cancel_signal.set(signal)
    try:
        with pytest.raises(IngestCancelled):
            asr._request_json("GET", "http://unused")
        assert calls == [1]
    finally:
        ingest_cancel_signal.reset(token)


def test_dashscope_nonretryable_failure_wins_over_concurrent_cancel(monkeypatch) -> None:
    signal = Event()

    def fail():
        raise requests.HTTPError("unauthorized")

    def request(*args, **kwargs):
        signal.set()
        return SimpleNamespace(status_code=401, raise_for_status=fail)

    monkeypatch.setattr(requests, "request", request)
    asr = video_asr.DashScopeFileTranscriptionVideoAsr(model_name="fake", base_url="unused")
    token = ingest_cancel_signal.set(signal)
    try:
        with pytest.raises(BackendError, match="unauthorized"):
            asr._request_json("POST", "http://unused")
    finally:
        ingest_cancel_signal.reset(token)


def test_video_normalizer_does_not_wrap_cancellation(tmp_path, monkeypatch) -> None:
    def cancel(*args):
        raise IngestCancelled("test cancellation")

    monkeypatch.setattr(video_pipeline, "run_video_memory_pipeline_off", cancel)
    with pytest.raises(IngestCancelled):
        VideoNormalizer()._run_pipeline(tmp_path / "fake.mp4", tmp_path)


def test_cancelled_poll_wait_returns_without_sleeping() -> None:
    signal = Event()
    signal.set()
    token = ingest_cancel_signal.set(signal)
    try:
        with pytest.raises(IngestCancelled):
            wait_ingest_cancelled(60)
    finally:
        ingest_cancel_signal.reset(token)


def test_dashscope_cancel_stops_polling(monkeypatch) -> None:
    signal = Event()
    calls = []
    monkeypatch.setattr(video_asr, "_upload_dashscope_audio", lambda *a, **kw: "oss://audio")
    asr = video_asr.DashScopeFileTranscriptionVideoAsr(
        model_name="fake", base_url="https://unused/api/v1", api_key="fake",
        poll_interval_seconds=60,
    )

    def request(method, *args, **kwargs):
        calls.append(method)
        if method == "POST":
            return {"output": {"task_id": "remote-task"}}
        signal.set()
        return {"output": {"task_status": "RUNNING"}}

    monkeypatch.setattr(asr, "_request_json", request)
    token = ingest_cancel_signal.set(signal)
    try:
        with pytest.raises(IngestCancelled):
            asr.transcribe(Path("fake.wav"), language=None, chunk_seconds=1)
        assert calls == ["POST", "GET"]
    finally:
        ingest_cancel_signal.reset(token)


def test_dashscope_cancel_stops_upload_retry(monkeypatch) -> None:
    signal = Event()
    calls = []

    def upload(**kwargs):
        calls.append(1)
        signal.set()
        raise OSError("upload interrupted")

    monkeypatch.setattr(
        video_asr, "_dashscope_oss_utils",
        lambda: (SimpleNamespace(upload=upload), RuntimeError),
    )
    token = ingest_cancel_signal.set(signal)
    try:
        with pytest.raises(IngestCancelled):
            video_asr._upload_dashscope_audio(
                Path("fake.wav"), model="fake", api_key="fake", base_url="https://unused"
            )
        assert calls == [1]
    finally:
        ingest_cancel_signal.reset(token)
