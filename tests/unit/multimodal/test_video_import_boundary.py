# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""All canonical API entry paths must enforce the same media source boundary."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from jiuwen_memory.api import assemble_runtime
from jiuwen_memory.common.errors import PermissionDeniedError
from jiuwen_memory.common.normalizer.normalizer_impl.video_normalizer import VideoNormalizer
from jiuwen_memory.common.security import internal_context
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory_entry.core.api_contract import invoke_api
from tests.support.scoped_authenticator import ScopedAuthenticator

pytestmark = pytest.mark.unit
SCOPE = Scope(org="acme", user="alice")


@pytest.fixture
def media_api(tmp_path, monkeypatch):
    settings = yaml.safe_load(
        (Path(__file__).parents[3] / "examples/config_multimodal.yml").read_text(encoding="utf-8")
    )["memory_api"]
    alice = tmp_path / "alice"
    bob = tmp_path / "bob"
    alice.mkdir()
    bob.mkdir()
    own = alice / "own.mp4"
    secret = bob / "private.mp4"
    own.write_bytes(b"admitted media")
    secret.write_bytes(b"other user's private media")
    params = settings["normalizer"]["default"]["params"]["routes"]["video"]["params"]
    params["temp_root"] = str(tmp_path / "tasks")
    params["local_media_roots"] = [
        {"scope": asdict(SCOPE), "root": str(alice)},
        {"scope": {"org": "acme", "user": "bob"}, "root": str(bob)},
    ]
    settings["llm"]["video_text"]["target"] = "echo"
    settings["llm"]["video_vision"]["target"] = "echo"
    calls = []

    def pipeline(_self, staged, _run_root):
        assert staged.read_bytes() == b"admitted media", "private media must never reach a model"
        calls.append(staged)
        return [{"id": "clip", "time_range": [0, 1], "ASR": "admitted transcript"}], []

    monkeypatch.setattr(VideoNormalizer, "_run_pipeline", pipeline)
    runtime = assemble_runtime(config=settings)
    security = internal_context(ScopedAuthenticator(SCOPE))
    try:
        yield runtime.api, security, own, secret, calls
    finally:
        runtime.close()


@pytest.mark.parametrize("verb", ["add", "add_async", "batch_add", "batch_add_async"])
def test_canonical_writes_preserve_valid_media_and_reject_other_user_file(media_api, verb):
    api, security, own, secret, calls = media_api

    def write(path):
        common = {"scope": asdict(SCOPE), "source": "video"}
        if verb.startswith("batch"):
            payload = {**common, "items": [{"content": path.as_uri()}]}
        else:
            payload = {**common, "content": path.as_uri()}
        return invoke_api(api, verb, payload, security)

    if verb.startswith("batch"):
        denied = write(secret)["outcomes"][0]
        assert denied["error_type"] == "PermissionDeniedError", "batch must record source denial"
        assert not denied["units"], "denied media must not create memory units"
    else:
        with pytest.raises(PermissionDeniedError):
            write(secret)
    assert not calls, "denied source must not reach models or subprocesses"
    result = write(own)
    assert "admitted transcript" in json.dumps(result), "valid media must still produce memory"
    assert len(calls) == 1, "valid media must run the original perception pipeline once"


def test_background_ingest_rejects_other_user_file_and_keeps_valid_job(media_api):
    api, security, own, secret, calls = media_api

    def submit(path, payload_id):
        submission = invoke_api(
            api,
            "submit_ingest",
            {
                "content": path.as_uri(),
                "source": "video",
                "scope": asdict(SCOPE),
                "payload_id": payload_id,
                "source_ref": path.as_uri(),
            },
            security,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = invoke_api(
                api,
                "job_status",
                {
                    "job_id": submission["job"]["id"],
                    "scope": asdict(SCOPE),
                },
                security,
            )
            if status["status"] in {"succeeded", "failed"}:
                return status
            time.sleep(0.01)
        raise TimeoutError("local mocked ingest job did not finish")

    denied = submit(secret, "denied-video")
    assert denied["status"] == "failed", "background ingest must preserve source denial"
    assert not denied["detail"]["unit_ids"], "denied source must not persist memories"
    assert not calls, "denied background source must not reach perception"
    accepted = submit(own, "accepted-video")
    assert accepted["status"] == "succeeded", "valid background video must remain supported"
    assert accepted["detail"]["unit_ids"], "valid job must expose its persisted memory ids"
    assert len(calls) == 1, "valid background video must run once"
