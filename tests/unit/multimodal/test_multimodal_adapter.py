from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from jiuwen_memory.api import build_kernel
from jiuwen_memory.common.normalizer.normalizer_impl.passthrough_normalizer import (
    PassthroughNormalizer,
)
from jiuwen_memory.common.normalizer.normalizer_impl.routing_normalizer import (
    RoutingNormalizer,
)
from jiuwen_memory.common.normalizer.normalizer_impl.video_normalizer import VideoNormalizer
from jiuwen_memory.common.type_def import (
    Context,
    FilterClause,
    FilterOp,
    Modality,
    RawPayload,
    Scope,
    iter_clauses,
)
from jiuwen_memory.config import Config
from jiuwen_memory.control.job_impl.ingest_job import InProcessIngestJobController
from jiuwen_memory.retrieval.base import RetrievalOperatorType
from jiuwen_memory.retrieval.retriever import Retriever
from jiuwen_memory.retrieval.retriever_impl.multimodal_retriever import (
    MultimodalRetriever,
)
from jiuwen_memory.retrieval.types import RetrievalQuery, RetrievalResult

_BOOTSTRAP_CORE = Path(__file__).parents[3] / "bootstrap" / "core"
if str(_BOOTSTRAP_CORE) not in sys.path:
    sys.path.append(str(_BOOTSTRAP_CORE))
handler = importlib.import_module("handler")

pytestmark = pytest.mark.unit


def _video_memory_output(_payload: RawPayload):
    return (
        [
            {
                "id": "clip-1",
                "time_range": [1.25, 30.75],
                "visual_summary": "A presenter opens a deployment diagram.",
                "detailed_caption": "A topology slide is visible.",
                "ASR": "Deploy the embedding service first.",
                "environment": "Meeting room",
            }
        ],
        [
            {
                "task_id": "event-1",
                "topic": "Deployment plan",
                "time_span": [1.25, 30.75],
                "narrative_summary": "The team validates before production.",
                "semantic_inference": "Testing is a release gate.",
                "child_clip_ids": ["clip-1"],
            }
        ],
    )


def test_routing_normalizer_keeps_text_and_routes_video() -> None:
    scope = Scope(user="user-1")
    normalizer = RoutingNormalizer(
        PassthroughNormalizer(),
        {Modality.VIDEO: VideoNormalizer(backend=_video_memory_output)},
    )

    text = normalizer.normalize(
        RawPayload(
            id="text-1",
            scope=scope,
            modality=Modality.TEXT,
            data=b"plain text",
        )
    )
    video = json.loads(
        normalizer.normalize(
            RawPayload(
                id="video-1",
                scope=scope,
                modality=Modality.VIDEO,
                uri="file:///data/demo.mp4",
            )
        )
    )

    assert text == "plain text"
    assert video["payload_id"] == "video-1"
    assert video["asset_uri"] == "file:///data/demo.mp4"
    assert video["clips"][0]["start_seconds"] == 1.25
    assert video["events"][0]["clip_ids"] == ["clip-1"]


def test_video_normalizer_uses_configured_temp_root(tmp_path, monkeypatch) -> None:
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"video")
    temp_root = tmp_path / "video-work"
    observed: dict[str, Path] = {}

    def fake_run_pipeline(self, source_path: Path, run_root: Path):
        del self
        observed["source_path"] = source_path
        observed["run_root"] = run_root
        assert run_root.parent == temp_root
        assert run_root.is_dir()
        return [], []

    monkeypatch.setattr(VideoNormalizer, "_run_pipeline", fake_run_pipeline)
    normalizer = VideoNormalizer.from_config({"temp_root": str(temp_root)})
    normalizer.normalize(
        RawPayload(
            id="video-1",
            scope=Scope(user="user-1"),
            modality=Modality.VIDEO,
            uri=video_path.as_uri(),
        )
    )

    assert observed["source_path"] == video_path
    assert not observed["run_root"].exists()


def test_video_normalizer_passes_separate_llm_and_vlm_ports(tmp_path, monkeypatch) -> None:
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"video")
    observed: dict[str, object] = {}
    asr_service = object()
    text_llm = object()
    vision_llm = object()

    def fake_run_pipeline(
        source_path: Path,
        run_root: Path,
        config: Any,
    ):
        observed.update(
            source_path=source_path,
            run_root=run_root,
            asr_port=config.asr_port,
            asr_language=config.asr_language,
            asr_chunk_seconds=config.asr_chunk_seconds,
            llm_port=config.llm_port,
            vlm_port=config.vlm_port,
        )
        return {"short_term": [], "medium_term": []}

    monkeypatch.setitem(
        sys.modules,
        "jiuwen_memory.common.normalizer.normalizer_impl.video_pipeline",
        SimpleNamespace(
            VideoPipelineConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            run_video_memory_pipeline_off=fake_run_pipeline,
        ),
    )
    normalizer = VideoNormalizer.from_config(
        {
            "asr_language": "zh",
            "asr_chunk_seconds": 300,
            "temp_root": str(tmp_path / "video-work"),
        },
        asr_port=asr_service,
        llm_port=text_llm,
        vlm_port=vision_llm,
    )

    normalizer.normalize(
        RawPayload(
            id="video-1",
            scope=Scope(user="user-1"),
            modality=Modality.VIDEO,
            uri=video_path.as_uri(),
        )
    )

    assert observed["asr_port"] is asr_service
    assert observed["asr_language"] == "zh"
    assert observed["asr_chunk_seconds"] == 300
    assert observed["llm_port"] is text_llm
    assert observed["vlm_port"] is vision_llm


def test_video_caption_uses_vlm_plugin(tmp_path, monkeypatch) -> None:
    monkeypatch.delitem(
        sys.modules,
        "jiuwen_memory.common.normalizer.normalizer_impl.video_pipeline",
        raising=False,
    )
    monkeypatch.setitem(
        sys.modules,
        "jiuwen_memory.common.normalizer.normalizer_impl.video_asr",
        SimpleNamespace(
            VideoAsrConfig=object,
            VideoAsrService=object,
            run_video_asr=lambda *args, **kwargs: [],
        ),
    )
    from jiuwen_memory.common.normalizer.normalizer_impl.video_pipeline import (
        _offline_vu,
    )

    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"video")
    calls: list[tuple[list, dict]] = []

    class RecordingVLM:
        @staticmethod
        def chat(messages, **options):
            calls.append((messages, options))
            return '{"visual_summary":"demo"}'

    result = _offline_vu(clip_path, "describe", RecordingVLM())

    assert result == {"visual_summary": "demo"}
    content = calls[0][0][0].content
    assert isinstance(content, list)
    assert content[0] == {
        "type": "video_url",
        "video_url": {"url": clip_path.resolve().as_uri()},
    }
    assert content[1] == {"type": "text", "text": "describe"}
    assert calls[0][1] == {"max_tokens": 512, "temperature": 0.4}


class _RecordingRetriever(Retriever):
    def __init__(self) -> None:
        self.queries: list[RetrievalQuery] = []

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RETRIEVER

    def health(self) -> None:
        return None

    def retrieve(self, scope: Scope, query: RetrievalQuery) -> RetrievalResult:
        del scope
        self.queries.append(query)
        return RetrievalResult()


def test_multimodal_retriever_builds_three_filtered_branches() -> None:
    scope = Scope(org="org-1", space="space-a", user="user-1")
    other_space = Scope(org="org-1", space="space-b", user="user-1")
    base = _RecordingRetriever()
    retriever = MultimodalRetriever(base)

    retriever.retrieve(
        scope,
        RetrievalQuery(
            text="video",
            filters=FilterClause("tags", FilterOp.CONTAINS, "keep"),
        ),
    )
    assert len(base.queries) == 3
    fields = [{clause.field for clause in iter_clauses(query.filters)} for query in base.queries]
    assert {"tags", "source"} in fields
    assert {
        "tags",
        "system_metadata.modal_type",
        "system_metadata.memory_level",
    } in fields

    base.queries.clear()
    retriever.retrieve(other_space, RetrievalQuery(text="video"))
    assert len(base.queries) == 3


def test_multimodal_config_add_and_search_end_to_end(tmp_path, monkeypatch) -> None:
    settings = yaml.safe_load(
        (Path(__file__).parents[3] / "examples" / "config_multimodal.yml").read_text(
            encoding="utf-8"
        )
    )["memory_api"]
    settings["normalizer"]["default"]["params"]["routes"]["video"]["params"][
        "temp_root"
    ] = str(tmp_path / "video-work")
    # Pipeline 被 mock，两个模型端口只需用 echo 完成装配。
    settings["llm"]["video_text"]["target"] = "echo"
    settings["llm"]["video_vision"]["target"] = "echo"

    def fake_run_pipeline(self, video_path: Path, run_root: Path):
        del self, video_path, run_root
        return _video_memory_output(
            RawPayload(id="video-1", scope=Scope(user="user-1"))
        )

    monkeypatch.setattr(VideoNormalizer, "_run_pipeline", fake_run_pipeline)
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"video")
    kernel = build_kernel(config=Config.from_dict(settings))
    scope = Scope(org="org-1", user="user-1")
    units = kernel.api.add(
        video_path.as_uri(),
        scope,
        Modality.VIDEO,
        identity=scope,
        assets=[video_path.as_uri()],
        system_metadata={
            "infer": "true",
            "pipeline": "video",
            "payload_id": "video-1",
        },
    )

    assert {unit.system_metadata["memory_level"] for unit in units} == {"clm", "elm"}
    result = kernel.api.search(
        "deployment",
        Context(scope),
        identity=scope,
        top_k=10,
        with_trajectory=True,
    )
    assert result.items
    assert {item.unit_id for item in result.items}.issubset({unit.id for unit in units})
    assert {
        step.detail.get("branch")
        for step in result.trajectory
        if step.detail.get("branch")
    } >= {"native", "multimodal_clip", "multimodal_event"}


def test_video_add_and_prefixed_job_status_share_handler_route(
    tmp_path, monkeypatch
) -> None:
    settings = yaml.safe_load(
        (Path(__file__).parents[3] / "examples" / "config_multimodal.yml").read_text(
            encoding="utf-8"
        )
    )["memory_api"]
    settings["normalizer"]["default"]["params"]["routes"]["video"]["params"][
        "temp_root"
    ] = str(tmp_path / "video-work")
    # Pipeline 被 mock，两个模型端口只需用 echo 完成装配。
    settings["llm"]["video_text"]["target"] = "echo"
    settings["llm"]["video_vision"]["target"] = "echo"
    monkeypatch.setattr(
        VideoNormalizer,
        "_run_pipeline",
        lambda self, video_path, run_root: _video_memory_output(
            RawPayload(id="video-1", scope=Scope(user="user-1"))
        ),
    )
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"video")
    kernel = build_kernel(config=Config.from_dict(settings))
    controller = kernel.ingest_jobs
    srv = type(
        "ServerStub",
        (),
        {
            "api": kernel.api,
            "ingest_jobs": controller,
            "config": SimpleNamespace(settings={"memory_api": settings}),
        },
    )()
    try:
        status, submitted = handler.dispatch(
            srv,
            "add",
            {
                "tenant_id": "org-1",
                "scope": "user-1",
                "payload_id": "video-1",
                "modality": "video",
                "uri": video_path.as_uri(),
            },
        )
        assert status == 200
        assert submitted["accepted"] is True
        assert submitted["job_id"].startswith("ing_")
        assert submitted["status"] in {"pending", "running", "succeeded"}
        assert submitted["reused"] is False

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, result = handler.dispatch(
                srv,
                "job",
                {
                    "tenant_id": "org-1",
                    "scope": "user-1",
                    "job_id": submitted["job_id"],
                },
            )
            if result.get("status") == "succeeded":
                break
            time.sleep(0.01)
        assert status == 200
        assert result["status"] == "succeeded"
        assert result["item_ids"]
        assert {item["system_metadata"]["video_id"] for item in result["items"]} == {
            "video-1"
        }

        status, reused = handler.dispatch(
            srv,
            "add",
            {
                "tenant_id": "org-1",
                "scope": "user-1",
                "payload_id": "video-1",
                "modality": "video",
                "uri": video_path.as_uri(),
            },
        )
        assert status == 200
        assert reused["job_id"] == submitted["job_id"]
        assert reused["status"] == "succeeded"
        assert reused["reused"] is True
    finally:
        controller.close()


def test_video_add_requires_uri() -> None:
    srv = type("ServerStub", (), {})()
    status, body = handler.dispatch(
        srv,
        "add",
        {
            "tenant_id": "org-1",
            "scope": "user-1",
            "modality": "video",
            "content": "file:///data/demo.mp4",
        },
    )

    assert status == 400
    assert body["error"] == "ValidationError"
    assert "uri" in body["message"]


def test_video_add_rejected_when_chain_not_assembled() -> None:
    """#4: 无多模态装配时 modality=video+uri 返回 400，不创建 ingest job。"""
    controller = InProcessIngestJobController(max_workers=1, max_pending_jobs=1)
    try:
        srv = SimpleNamespace(
            config=SimpleNamespace(settings={}),
            ingest_jobs=controller,
        )
        status, body = handler.dispatch(
            srv,
            "add",
            {
                "tenant_id": "org-1",
                "scope": "user-1",
                "payload_id": "video-1",
                "modality": "video",
                "uri": "file:///data/demo.mp4",
            },
        )

        assert status == 400
        assert body["error"] == "ValidationError"
        assert "video ingest requires" in body["message"]
        assert not vars(controller).get("_jobs")
    finally:
        controller.close()


def test_video_add_rejected_when_write_permission_denied() -> None:
    """P1-2: 无权限 identity 在 submit 前被 check_write 拒绝（403），不创建 ingest job。"""
    from jiuwen_memory.common.errors import PermissionDeniedError

    class _DeniedAPI:
        @staticmethod
        def check_write(
            scope,
            identity,
            *,
            tags=None,
            system_metadata=None,
            user_metadata=None,
        ):
            del scope, identity, tags, system_metadata, user_metadata
            raise PermissionDeniedError("write")

    controller = InProcessIngestJobController(max_workers=1, max_pending_jobs=1)
    try:
        memory_api_cfg = {
            "normalizer": {"default": {"params": {"routes": {"video": {}}}}},
            "evolver": {"video": {}},
        }
        srv = SimpleNamespace(
            config=SimpleNamespace(settings={"memory_api": memory_api_cfg}),
            api=_DeniedAPI(),
            ingest_jobs=controller,
        )
        status, body = handler.dispatch(
            srv,
            "add",
            {
                "tenant_id": "org-1",
                "scope": "user-1",
                "payload_id": "video-1",
                "modality": "video",
                "uri": "file:///data/demo.mp4",
            },
        )

        assert status == 403
        assert body["error"] == "PermissionDeniedError"
        assert not vars(controller).get("_jobs")
    finally:
        controller.close()


def test_ingest_job_status_uses_memory_api_read_permission() -> None:
    """视频任务状态必须经过 MemoryAPI 的 READ 鉴权。"""
    kernel = build_kernel()
    owner = Scope(org="org-1", user="owner")
    outsider = Scope(org="org-1", user="outsider")
    submission = kernel.ingest_jobs.submit(
        payload_id="video-1",
        source_ref="file:///data/demo.mp4",
        scope=owner,
        task=lambda: [],
    )
    srv = SimpleNamespace(api=kernel.api, ingest_jobs=kernel.ingest_jobs)
    try:
        status, body = handler.dispatch(
            srv,
            "job",
            {
                "tenant_id": owner.org,
                "scope": owner.user,
                "actor_tenant_id": outsider.org,
                "actor_scope": outsider.user,
                "job_id": submission.job.id,
            },
        )

        assert status == 403
        assert body["error"] == "PermissionDeniedError"
    finally:
        kernel.ingest_jobs.close()
