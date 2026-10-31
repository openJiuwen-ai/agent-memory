# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Local media source authorization, safe opening and unchanged video projections."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from jiuwen_memory.common.errors import PermissionDeniedError, ValidationError
from jiuwen_memory.common.normalizer.normalizer_impl.video_normalizer import VideoNormalizer
from jiuwen_memory.common.security.protection.local_media import LocalMediaImports
from jiuwen_memory.common.type_def import Modality, RawPayload, Scope

pytestmark = pytest.mark.unit
SCOPE = Scope(org="acme", space="project-a", user="alice", agent="bot", session="s1")


def _policy(root: Path) -> LocalMediaImports:
    return LocalMediaImports([{"scope": vars(SCOPE), "root": str(root)}])


def _paths(tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    source = root / "meeting video.mp4"
    source.write_bytes(b"authorized media")
    work = tmp_path / "work"
    work.mkdir()
    return root, source, work


@pytest.mark.parametrize("as_uri", [False, True])
def test_local_media_keeps_authorized_bytes_and_original_file(tmp_path, as_uri):
    root, source, work = _paths(tmp_path)
    staged = _policy(root).stage(source.as_uri() if as_uri else str(source), SCOPE, work)
    assert staged.parent == work / "input", "tools must receive only the task-private copy"
    assert staged.read_bytes() == source.read_bytes() == b"authorized media", (
        "media must stay intact"
    )


def test_local_media_denies_unconfigured_scope(tmp_path):
    _, source, work = _paths(tmp_path)
    with pytest.raises(PermissionDeniedError):
        LocalMediaImports().stage(source.as_uri(), SCOPE, work)
    assert not list(work.iterdir()), "denied input must not be staged"


@pytest.mark.parametrize("dimension", ["org", "space", "user", "agent", "session"])
def test_local_media_requires_exact_scope_in_every_dimension(tmp_path, dimension):
    root, source, work = _paths(tmp_path)
    other = replace(SCOPE, **{dimension: "another"})
    with pytest.raises(PermissionDeniedError):
        _policy(root).stage(source.as_uri(), other, work)
    assert not list(work.iterdir()), "another scope must never acquire this media"


def test_local_media_denies_sibling_prefix_and_external_paths(tmp_path):
    root, _, work = _paths(tmp_path)
    sibling = tmp_path / "imports-private"
    sibling.mkdir()
    secret = sibling / "private.mp4"
    secret.write_bytes(b"private media")
    with pytest.raises(PermissionDeniedError) as denied:
        _policy(root).stage(secret.as_uri(), SCOPE, work)
    assert str(secret) not in str(denied.value), "denials must not expose server paths"


@pytest.mark.parametrize("uri", ["file://other-host/share/video.mp4", "https://host/video.mp4"])
def test_local_media_rejects_network_sources(tmp_path, uri):
    root, _, work = _paths(tmp_path)
    with pytest.raises(ValidationError):
        _policy(root).stage(uri, SCOPE, work)


def test_local_media_rejects_parent_traversal_even_back_into_root(tmp_path):
    root, source, work = _paths(tmp_path)
    traversal = str(root / ".." / root.name / source.name)
    with pytest.raises(PermissionDeniedError):
        _policy(root).stage(traversal, SCOPE, work)


def test_local_media_rejects_directory_instead_of_regular_file(tmp_path):
    root, _, work = _paths(tmp_path)
    with pytest.raises(PermissionDeniedError):
        _policy(root).stage(root.as_uri(), SCOPE, work)


@pytest.mark.parametrize("bindings", ["/tmp", {}, [None], [{"scope": {}, "root": "/tmp"}]])
def test_local_media_rejects_malformed_server_policy(bindings):
    with pytest.raises(ValidationError):
        LocalMediaImports(bindings)


def test_local_media_does_not_treat_missing_scope_dimensions_as_wildcards(tmp_path):
    root, source, work = _paths(tmp_path)
    policy = LocalMediaImports([{"scope": {"org": "acme"}, "root": str(root)}])
    with pytest.raises(PermissionDeniedError):
        policy.stage(source.as_uri(), SCOPE, work)


def test_local_media_denies_symlink_outside_root(tmp_path):
    root, source, work = _paths(tmp_path)
    alias = root / "alias.mp4"
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"private")
    try:
        alias.symlink_to(outside)
    except OSError:
        pytest.skip("host does not allow symlink creation")
    with pytest.raises(PermissionDeniedError):
        _policy(root).stage(alias.as_uri(), SCOPE, work)
    assert source.read_bytes() == b"authorized media", "valid source must remain intact"


def test_local_media_denies_leaf_replaced_after_resolution(tmp_path, monkeypatch):
    root, source, work = _paths(tmp_path)
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"private")
    probe = root / "probe.mp4"
    try:
        probe.symlink_to(outside)
        probe.unlink()
    except OSError:
        pytest.skip("host does not allow symlink creation")
    policy = _policy(root)
    real_resolve = Path.resolve

    def resolve_then_replace(path, *args, **kwargs):
        resolved = real_resolve(path, *args, **kwargs)
        if path == source:
            source.unlink()
            source.symlink_to(outside)
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_then_replace)
    with pytest.raises(PermissionDeniedError):
        policy.stage(source.as_uri(), SCOPE, work)
    assert not list(work.iterdir()), "the replacement target must never be read"


def test_video_pipeline_reads_private_copy_and_preserves_asset_reference(tmp_path, monkeypatch):
    root, source, _ = _paths(tmp_path)
    observed = []

    def pipeline(_self, staged, run_root):
        source.write_bytes(b"source replaced after staging")
        assert staged.read_bytes() == b"authorized media", (
            "pipeline must read the admitted snapshot"
        )
        observed.append(run_root)
        return [], []

    monkeypatch.setattr(VideoNormalizer, "_run_pipeline", pipeline)
    normalizer = VideoNormalizer(
        local_media_roots=[{"scope": vars(SCOPE), "root": str(root)}],
        temp_root=str(tmp_path / "tasks"),
    )
    result = json.loads(
        normalizer.normalize(
            RawPayload(
                id="video-1",
                scope=SCOPE,
                modality=Modality.VIDEO,
                uri=source.as_uri(),
            )
        )
    )
    assert result["asset_uri"] == source.as_uri(), "original business reference must remain stable"
    assert result["payload_id"] == "video-1", "payload identity must remain stable"
    assert not observed[0].exists(), "private media copy must be cleaned up with the task"


def test_video_without_import_policy_never_calls_pipeline(tmp_path, monkeypatch):
    _, source, _ = _paths(tmp_path)

    def unexpected_pipeline(*_args):
        pytest.fail("unconfigured video input must not reach models or subprocesses")

    monkeypatch.setattr(VideoNormalizer, "_run_pipeline", unexpected_pipeline)
    with pytest.raises(PermissionDeniedError):
        VideoNormalizer(temp_root=str(tmp_path / "tasks")).normalize(
            RawPayload(
                id="video-1",
                scope=SCOPE,
                modality=Modality.VIDEO,
                uri=source.as_uri(),
            )
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction admission regression")
def test_local_media_denies_parent_junction_replaced_after_resolution(tmp_path, monkeypatch):
    import _winapi

    root, source, work = _paths(tmp_path)
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / source.name).write_bytes(b"another tenant's video")
    real_resolve = Path.resolve
    policy = _policy(root)

    def resolve_then_replace_parent(path, *args, **kwargs):
        resolved = real_resolve(path, *args, **kwargs)
        if path == source:
            root.rename(tmp_path / "original")
            _winapi.CreateJunction(str(outside), str(root))
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_then_replace_parent)
    with pytest.raises(PermissionDeniedError):
        policy.stage(source.as_uri(), SCOPE, work)
    assert not list(work.iterdir()), "a swapped parent must not disclose another tenant's media"


def test_local_media_closes_descriptors_when_stream_wrapper_fails(tmp_path, monkeypatch):
    root, source, work = _paths(tmp_path)
    policy = _policy(root)
    opened = set()
    real_open = os.open

    def record_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def fail_wrapper(descriptor, *_args, **_kwargs):
        opened.add(descriptor)
        raise OSError("stream wrapper could not be constructed")

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "fdopen", fail_wrapper)
    with pytest.raises(PermissionDeniedError):
        policy.stage(source.as_uri(), SCOPE, work)
    assert opened, "the regression must fail after acquiring a descriptor"
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)
