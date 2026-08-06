"""wikimem-compatible Markdown memory directory helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MAX_MEMORY_FILES = 200
DEFAULT_TOP_K = 5
FRONTMATTER_MAX_LINES = 30
MAX_MEMORY_LINES = 200
MAX_MEMORY_BYTES = 4096
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25000
MIN_HEADER_SELECTION_SCORE = 4.0
MIN_BODY_SELECTION_SCORE = 4.5
RELATIVE_SELECTION_SCORE_RATIO = 0.45
ENTRYPOINT_FILENAME = "MEMORY.md"

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_DAILY_LOG_RE = re.compile(r"^logs/[0-9]{4}/[0-9]{2}/[0-9]{4}-[0-9]{2}-[0-9]{2}\.md$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:/")
_SECRET_MARKERS = (
    "-----begin",
    "private key-----",
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
_WARNING_SIGNALS = {
    "warning",
    "warnings",
    "gotcha",
    "gotchas",
    "issue",
    "issues",
    "bug",
    "bugs",
    "risk",
    "risks",
    "known",
}


@dataclass(frozen=True)
class WikimemDirectory:
    """A wikimem memory directory."""

    scope: str
    path: str


@dataclass(frozen=True)
class MemoryFileHeader:
    """Header-stage Markdown memory candidate."""

    filename: str
    file_path: str
    mtime_ms: int
    description: str | None = None
    memory_type: str | None = None


@dataclass(frozen=True)
class RecalledMemoryFile:
    """Selected memory file before body materialization."""

    scope: str
    filename: str
    file_path: str
    mtime_ms: int
    description: str | None = None
    memory_type: str | None = None
    score: float = 0.0


@dataclass(frozen=True)
class RetrievedMemoryFile:
    """Materialized Markdown memory file."""

    scope: str
    filename: str
    file_path: str
    mtime_ms: int
    content: str
    description: str | None = None
    memory_type: str | None = None


@dataclass(frozen=True)
class RetrievedMemoryEntrypoint:
    """Materialized MEMORY.md entrypoint."""

    scope: str
    file_path: str
    content: str


def normalize_memory_relative_path(relative_path: str) -> str:
    """Normalize a memory relative path while preserving leading traversal."""

    normalized: list[str] = []
    value = relative_path.replace("\\", "/")
    for raw_part in value.split("/"):
        if raw_part in ("", "."):
            continue
        if raw_part == "..":
            if normalized and normalized[-1] != "..":
                normalized.pop()
            else:
                normalized.append("..")
        else:
            normalized.append(raw_part)
    return "/".join(normalized)


def should_include_memory_topic_file(relative_path: str) -> bool:
    """Return whether a relative path is a wikimem topic Markdown file."""

    value = relative_path.replace("\\", "/")
    normalized = normalize_memory_relative_path(value)
    if value.startswith("/") or _DRIVE_RE.match(value):
        return False
    if not normalized.endswith(".md"):
        return False
    if normalized == ENTRYPOINT_FILENAME:
        return False
    if ".." in normalized.split("/"):
        return False
    return _DAILY_LOG_RE.match(normalized) is None


def scan_memory_directory(
    memory_dir: str | Path, max_files: int = DEFAULT_MAX_MEMORY_FILES
) -> list[MemoryFileHeader]:
    """Scan Markdown topic files under a memory directory."""

    root = Path(memory_dir)
    if not root.is_dir():
        return []

    headers: list[MemoryFileHeader] = []
    for path in root.rglob("*.md"):
        relative = path.relative_to(root).as_posix()
        if not should_include_memory_topic_file(relative):
            continue
        try:
            headers.append(_read_memory_header(root, path))
        except OSError:
            continue

    headers.sort(key=lambda item: (-item.mtime_ms, item.filename))
    return headers[: max(0, max_files)]


def format_memory_manifest(memories: list[MemoryFileHeader]) -> str:
    """Render selector-facing memory manifest text."""

    lines: list[str] = []
    for memory in memories:
        type_tag = f"[{memory.memory_type}] " if memory.memory_type else ""
        timestamp = _format_iso8601_ms(memory.mtime_ms)
        if memory.description:
            lines.append(f"- {type_tag}{memory.filename} ({timestamp}): {memory.description}")
        else:
            lines.append(f"- {type_tag}{memory.filename} ({timestamp})")
    return "\n".join(lines)


def select_relevant_memory_files(
    query: str, memories: list[MemoryFileHeader], top_k: int
) -> list[MemoryFileHeader]:
    """Select high-confidence memories from headers."""

    scored = []
    for memory in memories:
        score = _score_header(query, memory)
        if score is not None:
            scored.append((score, memory.mtime_ms, memory))
    return [memory for _, _, memory in _select_confident(scored, top_k, MIN_HEADER_SELECTION_SCORE)]


def recall_relevant_memory_files_from_headers(
    query: str,
    memories: list[MemoryFileHeader],
    top_k: int,
    scope: str = "auto",
) -> list[RecalledMemoryFile]:
    """Select preloaded headers without rescanning directories."""

    scored = []
    for memory in memories:
        score = _score_header(query, memory)
        if score is not None:
            scored.append((score, memory.mtime_ms, memory))
    return [
        _recalled_from_header(memory, scope, score)
        for score, _, memory in _select_confident(scored, top_k, MIN_HEADER_SELECTION_SCORE)
    ]


def load_relevant_memory_files(
    query: str,
    directories: list[WikimemDirectory],
    top_k: int = DEFAULT_TOP_K,
    recent_tools: list[str] | None = None,
    already_surfaced_file_paths: list[str] | None = None,
) -> list[RetrievedMemoryFile]:
    """Scan, select, and materialize relevant wikimem Markdown files."""

    candidates = _collect_candidates(
        directories, recent_tools or [], already_surfaced_file_paths or []
    )
    header_selected = [
        _recalled_from_header(memory, scope, _score_header(query, memory) or 0.0)
        for memory, scope in candidates
        if _score_header(query, memory) is not None
    ]
    selected = _select_confident_recalled(header_selected, top_k, MIN_HEADER_SELECTION_SCORE)
    if not selected:
        body_scored = []
        for memory, scope in candidates:
            score = _score_body(query, memory.file_path)
            if score is not None:
                body_scored.append(_recalled_from_header(memory, scope, score))
        selected = _select_confident_recalled(body_scored, top_k, MIN_BODY_SELECTION_SCORE)
    return materialize_recalled_memory_files(selected)


def materialize_recalled_memory_files(files: list[RecalledMemoryFile]) -> list[RetrievedMemoryFile]:
    """Load selected file bodies with wikimem truncation limits."""

    materialized: list[RetrievedMemoryFile] = []
    for file in files:
        if file.scope == "team" and _content_has_potential_secrets(file.file_path):
            continue
        content = _read_limited_text(file.file_path, MAX_MEMORY_LINES, MAX_MEMORY_BYTES)
        materialized.append(
            RetrievedMemoryFile(
                scope=file.scope,
                filename=file.filename,
                file_path=file.file_path,
                mtime_ms=file.mtime_ms,
                content=content,
                description=file.description,
                memory_type=file.memory_type,
            )
        )
    return materialized


def load_memory_entrypoints(
    directories: list[WikimemDirectory],
) -> list[RetrievedMemoryEntrypoint]:
    """Load MEMORY.md entrypoints for directories that have one."""

    seen: set[str] = set()
    entrypoints: list[RetrievedMemoryEntrypoint] = []
    for directory in directories:
        path = Path(directory.path) / ENTRYPOINT_FILENAME
        normalized = str(path.resolve()) if path.exists() else str(path)
        if normalized in seen or not path.is_file():
            continue
        seen.add(normalized)
        if directory.scope == "team" and _content_has_potential_secrets(path):
            continue
        entrypoints.append(
            RetrievedMemoryEntrypoint(
                scope=directory.scope,
                file_path=str(path),
                content=_read_limited_text(path, MAX_ENTRYPOINT_LINES, MAX_ENTRYPOINT_BYTES),
            )
        )
    return entrypoints


def _read_memory_header(root: Path, path: Path) -> MemoryFileHeader:
    stat = path.stat()
    description, memory_type = _parse_frontmatter(path)
    return MemoryFileHeader(
        filename=path.relative_to(root).as_posix(),
        file_path=str(path),
        mtime_ms=stat.st_mtime_ns // 1_000_000,
        description=description,
        memory_type=memory_type,
    )


def _parse_frontmatter(path: Path) -> tuple[str | None, str | None]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None

    fields: dict[str, str] = {}
    index = 1
    while index < min(len(lines), FRONTMATTER_MAX_LINES + 1):
        line = lines[index]
        if line.strip() == "---":
            break
        if ":" not in line:
            index += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if value in (">", "|"):
            folded: list[str] = []
            index += 1
            while index < len(lines) and lines[index].startswith((" ", "\t")):
                folded.append(lines[index].strip())
                index += 1
            fields[key] = " ".join(part for part in folded if part)
            continue
        fields[key] = _strip_quotes(value)
        index += 1
    return fields.get("description") or None, fields.get("type") or None


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _collect_candidates(
    directories: list[WikimemDirectory],
    recent_tools: list[str],
    already_surfaced_file_paths: list[str],
) -> list[tuple[MemoryFileHeader, str]]:
    explicit_team_roots = [
        Path(directory.path).resolve()
        for directory in directories
        if directory.scope == "team" and Path(directory.path).exists()
    ]
    surfaced = {_normalize_file_path(path) for path in already_surfaced_file_paths}
    candidates: list[tuple[MemoryFileHeader, str]] = []
    for directory in directories:
        root = Path(directory.path)
        headers = scan_memory_directory(root)
        if directory.scope == "auto" and explicit_team_roots:
            filtered_headers = []
            for header in headers:
                is_team_header = False
                for team_root in explicit_team_roots:
                    if _path_starts_with(header.file_path, team_root):
                        is_team_header = True
                        break
                if not is_team_header:
                    filtered_headers.append(header)
            headers = filtered_headers
        for header in headers:
            if _normalize_file_path(header.file_path) in surfaced:
                continue
            if _is_recent_tool_reference(header, recent_tools):
                continue
            candidates.append((header, directory.scope))
    return candidates


def _select_confident(
    scored: list[tuple[float, int, MemoryFileHeader]], top_k: int, threshold: float
) -> list[tuple[float, int, MemoryFileHeader]]:
    if not scored:
        return []
    limit = DEFAULT_TOP_K if top_k == 0 else max(1, top_k)
    scored.sort(key=lambda item: (-item[0], -item[1], item[2].filename))
    best = scored[0][0]
    min_allowed = max(threshold, best * RELATIVE_SELECTION_SCORE_RATIO)
    return [item for item in scored if item[0] >= min_allowed][:limit]


def _select_confident_recalled(
    files: list[RecalledMemoryFile], top_k: int, threshold: float
) -> list[RecalledMemoryFile]:
    if not files:
        return []
    limit = DEFAULT_TOP_K if top_k == 0 else max(1, top_k)
    ordered = sorted(files, key=lambda item: (-item.score, -item.mtime_ms, item.filename))
    best = ordered[0].score
    min_allowed = max(threshold, best * RELATIVE_SELECTION_SCORE_RATIO)
    return [item for item in ordered if item.score >= min_allowed][:limit]


def _recalled_from_header(
    memory: MemoryFileHeader, scope: str, score: float
) -> RecalledMemoryFile:
    return RecalledMemoryFile(
        scope=scope,
        filename=memory.filename,
        file_path=memory.file_path,
        mtime_ms=memory.mtime_ms,
        description=memory.description,
        memory_type=memory.memory_type,
        score=score,
    )


def _score_header(query: str, memory: MemoryFileHeader) -> float | None:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return None
    filename_tokens = _tokenize(memory.filename)
    description_tokens = _tokenize(memory.description or "")
    type_tokens = _tokenize(memory.memory_type or "")
    score = 0.0
    for token in query_tokens:
        if token in filename_tokens:
            score += 1.2
        if token in description_tokens:
            score += 1.5
        if token in type_tokens:
            score += 0.5
    if memory.memory_type == "feedback" and {"test", "safe", "safety"} & set(query_tokens):
        score += 0.8
    return score if score > 0 else None


def _score_body(query: str, file_path: str | Path) -> float | None:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return None
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    body_tokens = _tokenize(content)
    score = sum(1.5 for token in query_tokens if token in body_tokens)
    return score if score > 0 else None


def _tokenize(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(text.lower()):
        normalized = _normalize_token(token)
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def _normalize_token(token: str) -> str:
    if len(token) > 5 and token.endswith("ing"):
        token = token[:-3]
    if len(token) > 4 and token.endswith("ly"):
        token = token[:-2]
    if len(token) > 3 and token.endswith("ies"):
        token = f"{token[:-3]}y"
    if len(token) > 3 and token.endswith("s"):
        token = token[:-1]
    if token == "safety":
        return "safe"
    return token


def _is_recent_tool_reference(header: MemoryFileHeader, recent_tools: list[str]) -> bool:
    if header.memory_type != "reference" or not recent_tools:
        return False
    haystack = " ".join([header.filename, header.description or ""]).lower()
    tokens = set(_tokenize(haystack))
    if tokens & _WARNING_SIGNALS:
        return False
    return any(_normalize_token(tool.lower()) in tokens for tool in recent_tools)


def _content_has_potential_secrets(path: str | Path) -> bool:
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    if "-----begin" in content and "private key-----" in content:
        return True
    return any(marker in content for marker in _SECRET_MARKERS if not marker.startswith("-----"))


def _read_limited_text(path: str | Path, max_lines: int, max_bytes: int) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    content = "\n".join(lines)
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        content = encoded[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True
    if truncated:
        content = f"{content}\n[truncated]"
    return content


def _format_iso8601_ms(mtime_ms: int) -> str:
    return datetime.fromtimestamp(mtime_ms / 1000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _normalize_file_path(path: str) -> str:
    return str(Path(path).expanduser()).replace("\\", "/").lower()


def _path_starts_with(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root)
        return True
    except ValueError:
        return False
