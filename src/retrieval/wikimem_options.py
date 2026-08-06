"""wikimem compatibility option parsing for retrieval adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from common.errors import ValidationError

KEY_RECENT_TOOLS = "wikimem.recent_tools"
KEY_ALREADY_SURFACED_FILE_PATHS = "wikimem.already_surfaced_file_paths"
KEY_INCLUDE_ENTRYPOINTS = "wikimem.include_entrypoints"
KEY_MEMORY_DIRS = "wikimem.memory_dirs"
KEY_PROFILE = "wikimem.profile"
KEY_SELECTOR_MODEL = "wikimem.selector_model"
KEY_SELECTOR_FALLBACK_MODEL = "wikimem.selector_fallback_model"
KEY_MEMORY_PARALLELISM = "wikimem.memory_parallelism"

_VALID_MEMORY_DIR_SCOPES = {"auto", "team"}


@dataclass(frozen=True)
class WikimemDirectory:
    """A wikimem Markdown memory directory declaration."""

    scope: str
    path: str


@dataclass(frozen=True)
class WikimemRetrievalOptions:
    """Parsed wikimem retrieval options carried by ``Context.extensions``."""

    recent_tools: list[str] = field(default_factory=list)
    already_surfaced_file_paths: list[str] = field(default_factory=list)
    include_entrypoints: bool = False
    memory_dirs: list[WikimemDirectory] = field(default_factory=list)
    profile: str = ""
    selector_model: str = ""
    selector_fallback_model: str = ""
    memory_parallelism: int | None = None


def parse_wikimem_options(extensions: dict[str, str] | None) -> WikimemRetrievalOptions:
    """Parse transport-safe ``wikimem.*`` extension values.

    Core mem2.0 ignores these keys. wikimem-compatible adapters call this helper
    at their boundary and receive typed values plus explicit configuration errors.
    """

    values = extensions or {}
    return WikimemRetrievalOptions(
        recent_tools=_parse_string_array(values, KEY_RECENT_TOOLS),
        already_surfaced_file_paths=_parse_string_array(
            values, KEY_ALREADY_SURFACED_FILE_PATHS
        ),
        include_entrypoints=_parse_bool(values, KEY_INCLUDE_ENTRYPOINTS, default=False),
        memory_dirs=_parse_memory_dirs(values),
        profile=values.get(KEY_PROFILE, "").strip(),
        selector_model=values.get(KEY_SELECTOR_MODEL, "").strip(),
        selector_fallback_model=values.get(KEY_SELECTOR_FALLBACK_MODEL, "").strip(),
        memory_parallelism=_parse_positive_floor_int(values, KEY_MEMORY_PARALLELISM),
    )


def _parse_json_value(values: dict[str, str], key: str) -> Any:
    raw = values.get(key)
    if raw is None or raw.strip() == "":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{key} must be valid JSON") from exc


def _parse_string_array(values: dict[str, str], key: str) -> list[str]:
    parsed = _parse_json_value(values, key)
    if parsed is None:
        return []
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValidationError(f"{key} must be a JSON string array")
    return list(parsed)


def _parse_bool(values: dict[str, str], key: str, *, default: bool) -> bool:
    raw = values.get(key)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValidationError(f"{key} must be \"true\" or \"false\"")


def _parse_positive_floor_int(values: dict[str, str], key: str) -> int | None:
    raw = values.get(key)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValidationError(f"{key} must be an integer string") from exc
    return max(1, value)


def _parse_memory_dirs(values: dict[str, str]) -> list[WikimemDirectory]:
    parsed = _parse_json_value(values, KEY_MEMORY_DIRS)
    if parsed is None:
        return []
    if not isinstance(parsed, list):
        raise ValidationError(f"{KEY_MEMORY_DIRS} must be a JSON array")

    dirs: list[WikimemDirectory] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValidationError(f"{KEY_MEMORY_DIRS}[{index}] must be an object")
        scope = item.get("scope")
        path = item.get("path")
        if not isinstance(scope, str) or scope not in _VALID_MEMORY_DIR_SCOPES:
            raise ValidationError(
                f"{KEY_MEMORY_DIRS}[{index}].scope must be one of auto, team"
            )
        if not isinstance(path, str) or not path.strip():
            raise ValidationError(f"{KEY_MEMORY_DIRS}[{index}].path must be non-empty")
        dirs.append(WikimemDirectory(scope=scope, path=path))
    return dirs
