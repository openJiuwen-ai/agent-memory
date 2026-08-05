"""wikimem team memory synchronization adapter."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

MAX_FILE_SIZE_BYTES = 250_000
MAX_PUT_BODY_BYTES = 200_000
MAX_CONFLICT_RETRIES = 2

_SECRET_MARKERS = (
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "sk-ant-",
    "sk-proj-",
    "sk-svcacct-",
    "sk-admin-",
    "xoxb-",
    "xoxp-",
    "xoxe-",
    "xapp-",
    "akia",
    "asia",
    "abia",
    "acca",
)


@dataclass
class SyncState:
    last_known_checksum: str | None = None
    server_checksums: dict[str, str] = field(default_factory=dict)
    server_max_entries: int | None = None


@dataclass(frozen=True)
class SkippedSecretFile:
    path: str
    reason: str


@dataclass(frozen=True)
class LocalTeamMemorySnapshot:
    entries: dict[str, str]
    skipped_secrets: list[SkippedSecretFile]


@dataclass(frozen=True)
class RemoteTeamMemoryData:
    checksum: str | None
    entries: dict[str, str]
    entry_checksums: dict[str, str]


@dataclass(frozen=True)
class FetchOutcome:
    kind: str
    checksum: str | None = None
    data: RemoteTeamMemoryData | None = None

    @classmethod
    def not_modified(cls, checksum: str | None = None) -> FetchOutcome:
        return cls(kind="not_modified", checksum=checksum)

    @classmethod
    def empty(cls) -> FetchOutcome:
        return cls(kind="empty")

    @classmethod
    def data(cls, data: RemoteTeamMemoryData) -> FetchOutcome:
        return cls(kind="data", data=data)


@dataclass(frozen=True)
class HashesProbe:
    checksum: str | None
    entry_checksums: dict[str, str]


@dataclass(frozen=True)
class PutOutcome:
    kind: str
    checksum: str | None = None
    max_entries: int | None = None
    received_entries: int | None = None

    @classmethod
    def success(cls, checksum: str | None = None) -> PutOutcome:
        return cls(kind="success", checksum=checksum)

    @classmethod
    def conflict(cls) -> PutOutcome:
        return cls(kind="conflict")

    @classmethod
    def too_many_entries(cls, max_entries: int, received_entries: int) -> PutOutcome:
        return cls(
            kind="too_many_entries",
            max_entries=max_entries,
            received_entries=received_entries,
        )


class TeamMemoryFailureKind(str, Enum):
    AUTH = "auth"
    TIMEOUT = "timeout"
    NETWORK = "network"
    CONFLICT = "conflict"
    NO_OAUTH = "no_oauth"
    NO_REPO = "no_repo"
    PARSE = "parse"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TeamMemorySyncFailure:
    kind: TeamMemoryFailureKind
    message: str
    http_status: int | None = None
    permanent: bool = False


@dataclass(frozen=True)
class PullOutcome:
    success: bool
    checksum: str | None
    not_modified: bool
    is_empty: bool
    files_written: int
    failure: TeamMemorySyncFailure | None = None


@dataclass(frozen=True)
class PushOutcomeSummary:
    success: bool
    files_uploaded: int
    checksum: str | None
    conflict: bool
    skipped_secrets: list[SkippedSecretFile]
    server_max_entries: int | None
    failure: TeamMemorySyncFailure | None = None


class TeamMemoryRemote(Protocol):
    async def fetch(self, repo_slug: str, if_none_match: str | None) -> FetchOutcome:
        """Fetch all remote team memory entries."""

    async def fetch_hashes(self, repo_slug: str) -> HashesProbe:
        """Fetch remote entry checksums."""

    async def put_entries(
        self, repo_slug: str, if_match: str | None, entries: dict[str, str]
    ) -> PutOutcome:
        """Upload a batch of team memory entries."""


async def pull_team_memory(
    remote: TeamMemoryRemote,
    state: SyncState,
    team_memory_root: str | os.PathLike[str],
    repo_slug: str,
    skip_etag_cache: bool = False,
) -> PullOutcome:
    if_none_match = None if skip_etag_cache else state.last_known_checksum
    try:
        fetch = await remote.fetch(repo_slug, if_none_match)
    except TeamMemorySyncFailure as failure:
        return _pull_failure(failure)
    except Exception as error:  # pragma: no cover - defensive adapter boundary
        return _pull_failure(_unknown_failure(str(error)))

    if fetch.kind == "not_modified":
        return PullOutcome(
            success=True,
            checksum=fetch.checksum,
            not_modified=True,
            is_empty=False,
            files_written=0,
        )
    if fetch.kind == "empty":
        state.last_known_checksum = None
        state.server_checksums.clear()
        return PullOutcome(
            success=True,
            checksum=None,
            not_modified=False,
            is_empty=True,
            files_written=0,
        )
    if fetch.kind != "data" or fetch.data is None:
        return _pull_failure(_unknown_failure(f"Unexpected fetch outcome: {fetch.kind}"))

    try:
        files_written = _write_remote_entries(Path(team_memory_root), fetch.data.entries)
    except Exception as error:
        return _pull_failure(_unknown_failure(f"Failed to write pulled team memory: {error}"))

    state.last_known_checksum = fetch.data.checksum
    if fetch.data.entry_checksums:
        state.server_checksums = dict(fetch.data.entry_checksums)
    else:
        state.server_checksums = {
            key: hash_content(value) for key, value in fetch.data.entries.items()
        }
    return PullOutcome(
        success=True,
        checksum=fetch.data.checksum,
        not_modified=False,
        is_empty=False,
        files_written=files_written,
    )


async def push_team_memory(
    remote: TeamMemoryRemote,
    state: SyncState,
    team_memory_root: str | os.PathLike[str],
    repo_slug: str,
) -> PushOutcomeSummary:
    try:
        local = read_local_team_memory(team_memory_root, state.server_max_entries)
    except Exception as error:
        return PushOutcomeSummary(
            success=False,
            files_uploaded=0,
            checksum=None,
            conflict=False,
            skipped_secrets=[],
            server_max_entries=state.server_max_entries,
            failure=_unknown_failure(f"Failed to read local team memory: {error}"),
        )

    local_hashes = {key: hash_content(value) for key, value in local.entries.items()}
    conflict_attempts = 0

    while True:
        delta = _compute_delta(local.entries, local_hashes, state.server_checksums)
        if not delta:
            return PushOutcomeSummary(
                success=True,
                files_uploaded=0,
                checksum=state.last_known_checksum,
                conflict=False,
                skipped_secrets=local.skipped_secrets,
                server_max_entries=state.server_max_entries,
            )

        files_uploaded = 0
        needs_retry = False
        for batch in batch_delta_by_bytes(delta, MAX_PUT_BODY_BYTES):
            try:
                outcome = await remote.put_entries(repo_slug, state.last_known_checksum, batch)
            except TeamMemorySyncFailure as failure:
                return PushOutcomeSummary(
                    success=False,
                    files_uploaded=files_uploaded,
                    checksum=state.last_known_checksum,
                    conflict=conflict_attempts > 0,
                    skipped_secrets=local.skipped_secrets,
                    server_max_entries=state.server_max_entries,
                    failure=failure,
                )
            except Exception as error:  # pragma: no cover - defensive adapter boundary
                return PushOutcomeSummary(
                    success=False,
                    files_uploaded=files_uploaded,
                    checksum=state.last_known_checksum,
                    conflict=conflict_attempts > 0,
                    skipped_secrets=local.skipped_secrets,
                    server_max_entries=state.server_max_entries,
                    failure=_unknown_failure(str(error)),
                )

            if outcome.kind == "success":
                if outcome.checksum is not None:
                    state.last_known_checksum = outcome.checksum
                for key in batch:
                    state.server_checksums[key] = local_hashes[key]
                files_uploaded += len(batch)
                continue

            if outcome.kind == "too_many_entries":
                state.server_max_entries = outcome.max_entries
                return PushOutcomeSummary(
                    success=False,
                    files_uploaded=files_uploaded,
                    checksum=state.last_known_checksum,
                    conflict=False,
                    skipped_secrets=local.skipped_secrets,
                    server_max_entries=state.server_max_entries,
                    failure=TeamMemorySyncFailure(
                        kind=TeamMemoryFailureKind.UNKNOWN,
                        message="Server rejected team memory: too many entries",
                        http_status=413,
                        permanent=True,
                    ),
                )

            if outcome.kind == "conflict":
                if conflict_attempts >= MAX_CONFLICT_RETRIES:
                    return PushOutcomeSummary(
                        success=False,
                        files_uploaded=files_uploaded,
                        checksum=state.last_known_checksum,
                        conflict=True,
                        skipped_secrets=local.skipped_secrets,
                        server_max_entries=state.server_max_entries,
                        failure=TeamMemorySyncFailure(
                            kind=TeamMemoryFailureKind.CONFLICT,
                            message="Team memory push conflicted after retries",
                            http_status=412,
                        ),
                    )
                try:
                    hashes = await remote.fetch_hashes(repo_slug)
                except TeamMemorySyncFailure as failure:
                    return PushOutcomeSummary(
                        success=False,
                        files_uploaded=files_uploaded,
                        checksum=state.last_known_checksum,
                        conflict=True,
                        skipped_secrets=local.skipped_secrets,
                        server_max_entries=state.server_max_entries,
                        failure=failure,
                    )
                state.last_known_checksum = hashes.checksum
                state.server_checksums = dict(hashes.entry_checksums)
                conflict_attempts += 1
                needs_retry = True
                break

            return PushOutcomeSummary(
                success=False,
                files_uploaded=files_uploaded,
                checksum=state.last_known_checksum,
                conflict=conflict_attempts > 0,
                skipped_secrets=local.skipped_secrets,
                server_max_entries=state.server_max_entries,
                failure=_unknown_failure(f"Unexpected put outcome: {outcome.kind}"),
            )

        if needs_retry:
            continue

        return PushOutcomeSummary(
            success=True,
            files_uploaded=files_uploaded,
            checksum=state.last_known_checksum,
            conflict=False,
            skipped_secrets=local.skipped_secrets,
            server_max_entries=state.server_max_entries,
        )


def read_local_team_memory(
    team_memory_root: str | os.PathLike[str],
    server_max_entries: int | None = None,
) -> LocalTeamMemorySnapshot:
    root = Path(team_memory_root)
    entries: dict[str, str] = {}
    skipped_secrets: list[SkippedSecretFile] = []
    if not root.exists():
        return LocalTeamMemorySnapshot(entries=entries, skipped_secrets=skipped_secrets)

    files = (candidate for candidate in root.rglob("*") if candidate.is_file())
    for path in sorted(files, key=str):
        if path.stat().st_size > MAX_FILE_SIZE_BYTES:
            continue
        relative = path.relative_to(root).as_posix()
        content = path.read_text(encoding="utf-8")
        if _has_potential_secret(content):
            skipped_secrets.append(SkippedSecretFile(path=relative, reason="potential_secret"))
            continue
        entries[relative] = content

    if server_max_entries is not None:
        entries = dict(sorted(entries.items())[:server_max_entries])
    else:
        entries = dict(sorted(entries.items()))
    return LocalTeamMemorySnapshot(entries=entries, skipped_secrets=skipped_secrets)


def batch_delta_by_bytes(delta: dict[str, str], max_body_bytes: int) -> list[dict[str, str]]:
    if not delta:
        return []

    batches: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for key, value in sorted(delta.items()):
        current[key] = value
        if _estimate_put_body_bytes(current) > max_body_bytes and len(current) > 1:
            current.pop(key)
            batches.append(current)
            current = {key: value}
    if current:
        batches.append(current)
    return batches


def hash_content(content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_relative_team_memory_key(key: str) -> str:
    if "\0" in key or "\\" in key or key.startswith("/") or _has_windows_prefix(key):
        raise ValueError(f"Invalid team memory key: {key}")

    decoded = urllib.parse.unquote(key)
    if decoded != key and (".." in decoded or "/" in decoded or "\\" in decoded):
        raise ValueError(f"Invalid team memory key: {key}")

    parts = key.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Invalid team memory key: {key}")
    return "/".join(parts)


def _write_remote_entries(team_memory_root: Path, entries: dict[str, str]) -> int:
    team_memory_root.mkdir(parents=True, exist_ok=True)
    files_written = 0
    for key, content in sorted(entries.items()):
        relative = validate_relative_team_memory_key(key)
        path = team_memory_root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        files_written += 1
    return files_written


def _compute_delta(
    local_entries: dict[str, str],
    local_hashes: dict[str, str],
    server_checksums: dict[str, str],
) -> dict[str, str]:
    return {
        key: value
        for key, value in sorted(local_entries.items())
        if server_checksums.get(key) != local_hashes.get(key)
    }


def _estimate_put_body_bytes(entries: dict[str, str]) -> int:
    body = json.dumps({"entries": dict(sorted(entries.items()))}, separators=(",", ":"))
    return len(body.encode("utf-8"))


def _has_potential_secret(value: str) -> bool:
    lower = value.lower()
    if "-----begin" in lower and "private key-----" in lower:
        return True
    return any(marker in lower for marker in _SECRET_MARKERS)


def _has_windows_prefix(key: str) -> bool:
    return len(key) >= 2 and key[1] == ":" and key[0].isalpha()


def _pull_failure(failure: TeamMemorySyncFailure) -> PullOutcome:
    return PullOutcome(
        success=False,
        checksum=None,
        not_modified=False,
        is_empty=False,
        files_written=0,
        failure=failure,
    )


def _unknown_failure(message: str) -> TeamMemorySyncFailure:
    return TeamMemorySyncFailure(
        kind=TeamMemoryFailureKind.UNKNOWN,
        message=message,
        permanent=False,
    )
