"""wikimem team memory sync compatibility tests."""

from __future__ import annotations

import asyncio
from collections import deque

import pytest

from agent_plugin.wikimem.team_sync import (
    FetchOutcome,
    HashesProbe,
    PutOutcome,
    RemoteTeamMemoryData,
    SyncState,
    TeamMemoryFailureKind,
    batch_delta_by_bytes,
    hash_content,
    pull_team_memory,
    push_team_memory,
    read_local_team_memory,
    validate_relative_team_memory_key,
)

pytestmark = pytest.mark.unit


class FakeRemote:
    def __init__(self, *, fetch_results=None, hash_results=None, put_results=None) -> None:
        self.fetch_results = deque(fetch_results or [])
        self.hash_results = deque(hash_results or [])
        self.put_results = deque(put_results or [])
        self.put_requests = []

    async def fetch(self, repo_slug: str, if_none_match: str | None):
        assert repo_slug == "owner/repo"
        return self.fetch_results.popleft()

    async def fetch_hashes(self, repo_slug: str):
        assert repo_slug == "owner/repo"
        return self.hash_results.popleft()

    async def put_entries(self, repo_slug: str, if_match: str | None, entries: dict[str, str]):
        assert repo_slug == "owner/repo"
        self.put_requests.append((if_match, dict(entries)))
        return self.put_results.popleft()


def test_hashes_content_with_sha256_prefix() -> None:
    assert (
        hash_content("hello")
        == "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_batches_delta_by_serialized_body_size() -> None:
    delta = {
        "a.md": "alpha" * 20,
        "b.md": "beta" * 20,
        "c.md": "gamma" * 20,
    }

    batches = batch_delta_by_bytes(delta, 120)

    assert [list(batch) for batch in batches] == [["a.md"], ["b.md"], ["c.md"]]


def test_validates_safe_relative_team_memory_keys() -> None:
    assert validate_relative_team_memory_key("nested/release.md") == "nested/release.md"

    for key in [
        "%2e%2e%2fsecret.md",
        "..\\secret.md",
        "/secret.md",
        "../secret.md",
        "C:/secret.md",
        "nested/%2fsecret.md",
    ]:
        with pytest.raises(ValueError):
            validate_relative_team_memory_key(key)


def test_pull_team_memory_handles_not_modified_empty_and_data(tmp_path) -> None:
    remote = FakeRemote(
        fetch_results=[
            FetchOutcome.not_modified("etag-1"),
            FetchOutcome.empty(),
            FetchOutcome.data(
                RemoteTeamMemoryData(
                    checksum="etag-4",
                    entries={
                        "MEMORY.md": "server memory",
                        "nested/patterns.md": "nested pattern",
                    },
                    entry_checksums={},
                )
            ),
        ]
    )
    state = SyncState(
        last_known_checksum="etag-1",
        server_checksums={"MEMORY.md": "sha256:1"},
    )

    first = asyncio.run(pull_team_memory(remote, state, tmp_path, "owner/repo"))
    assert first.success
    assert first.not_modified
    assert first.checksum == "etag-1"
    assert remote.fetch_results

    second = asyncio.run(
        pull_team_memory(remote, state, tmp_path, "owner/repo", skip_etag_cache=True)
    )
    assert second.success
    assert second.is_empty
    assert state.last_known_checksum is None
    assert state.server_checksums == {}

    third = asyncio.run(pull_team_memory(remote, state, tmp_path, "owner/repo"))
    assert third.success
    assert third.files_written == 2
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "server memory"
    assert (tmp_path / "nested" / "patterns.md").read_text(encoding="utf-8") == "nested pattern"
    assert state.server_checksums["MEMORY.md"] == hash_content("server memory")


def test_pull_team_memory_rejects_remote_path_traversal(tmp_path) -> None:
    remote = FakeRemote(
        fetch_results=[
            FetchOutcome.data(
                RemoteTeamMemoryData(
                    checksum="etag-bad",
                    entries={"../secret.md": "bad"},
                    entry_checksums={},
                )
            )
        ]
    )

    result = asyncio.run(pull_team_memory(remote, SyncState(), tmp_path, "owner/repo"))

    assert not result.success
    assert result.failure is not None
    assert result.failure.kind is TeamMemoryFailureKind.UNKNOWN
    assert not (tmp_path.parent / "secret.md").exists()


def test_push_team_memory_retries_conflicts_after_hash_probe(tmp_path) -> None:
    (tmp_path / "MEMORY.md").write_text("local memory", encoding="utf-8")
    (tmp_path / "patterns.md").write_text("shared pattern", encoding="utf-8")
    remote = FakeRemote(
        hash_results=[
            HashesProbe(
                checksum="etag-2",
                entry_checksums={
                    "MEMORY.md": hash_content("remote memory"),
                    "patterns.md": hash_content("shared pattern"),
                },
            )
        ],
        put_results=[
            PutOutcome.conflict(),
            PutOutcome.success("etag-3"),
        ],
    )
    state = SyncState(last_known_checksum="etag-1")

    result = asyncio.run(push_team_memory(remote, state, tmp_path, "owner/repo"))

    assert result.success
    assert result.files_uploaded == 1
    assert result.checksum == "etag-3"
    assert len(remote.put_requests) == 2
    assert len(remote.put_requests[0][1]) == 2
    assert list(remote.put_requests[1][1]) == ["MEMORY.md"]
    assert state.server_checksums["MEMORY.md"] == hash_content("local memory")
    assert state.server_checksums["patterns.md"] == hash_content("shared pattern")


def test_push_team_memory_learns_server_max_entries_from_413(tmp_path) -> None:
    for name, content in [("a.md", "alpha"), ("b.md", "beta"), ("c.md", "gamma")]:
        (tmp_path / name).write_text(content, encoding="utf-8")
    remote = FakeRemote(
        put_results=[PutOutcome.too_many_entries(max_entries=2, received_entries=3)]
    )
    state = SyncState()

    result = asyncio.run(push_team_memory(remote, state, tmp_path, "owner/repo"))

    assert not result.success
    assert result.server_max_entries == 2
    assert state.server_max_entries == 2
    assert result.failure is not None
    assert result.failure.http_status == 413


def test_push_team_memory_updates_checksums_across_multiple_batches(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.md").write_text("alpha " * 36_000, encoding="utf-8")
    (tmp_path / "b.md").write_text("beta " * 36_000, encoding="utf-8")
    monkeypatch.setattr("agent_plugin.wikimem.team_sync.MAX_PUT_BODY_BYTES", 200_000)
    remote = FakeRemote(
        put_results=[
            PutOutcome.success("etag-1"),
            PutOutcome.success("etag-2"),
        ]
    )
    state = SyncState()

    result = asyncio.run(push_team_memory(remote, state, tmp_path, "owner/repo"))

    assert result.success
    assert result.files_uploaded == 2
    assert len(remote.put_requests) == 2
    assert state.last_known_checksum == "etag-2"
    assert set(state.server_checksums) == {"a.md", "b.md"}


def test_read_local_team_memory_skips_potential_secret(tmp_path) -> None:
    (tmp_path / "safe.md").write_text("safe shared note", encoding="utf-8")
    (tmp_path / "secret.md").write_text("token: github_pat_secret", encoding="utf-8")

    snapshot = read_local_team_memory(tmp_path)

    assert snapshot.entries == {"safe.md": "safe shared note"}
    assert [(item.path, item.reason) for item in snapshot.skipped_secrets] == [
        ("secret.md", "potential_secret")
    ]
