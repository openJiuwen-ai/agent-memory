# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Durable Markdown blocks for shared experience units."""

import hashlib
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def.memory import MemoryUnit
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.common.type_def.scope import Scope

logger = get_logger(__name__)

FILE_NAME = "EXPERIENCE.md"
METADATA_NAME = ".EXPERIENCE.meta.json"
DOCUMENT_PATH = f"memory/experience/{FILE_NAME}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _body(unit: MemoryUnit) -> str:
    return unit.segments[0].content.replace("\r\n", "\n").replace("\r", "\n")


def _snapshot(source: str, units: Iterable[MemoryUnit]) -> dict:
    entries = []
    for unit in units:
        first, *rest = unit.segments
        metadata = replace(unit, segments=[replace(first, content=""), *rest])
        entries.append({"metadata": json.loads(dumps(metadata)), "sha256": _digest(_body(unit))})
    return {"source_sha256": _digest(source), "entries": entries}


def _split_bodies(source: str) -> list[str]:
    """Read ordinary Markdown list items, retaining indented continuation lines."""
    bodies: list[list[str]] = []
    for line in source.split("\n"):
        if line.startswith("- "):
            bodies.append([line[2:]])
        elif line.startswith("  ") and bodies:
            bodies[-1].append(line[2:])
        elif line.strip():
            raise ValueError("Experience document expects a list of experience items")
    return ["\n".join(lines) for lines in bodies]


def _read_sidecar(absolute: Path) -> dict | None:
    metadata = absolute.with_name(METADATA_NAME)
    if metadata.is_symlink():
        raise ValueError("Experience metadata cannot be a symlink")
    try:
        content = json.loads(metadata.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(content, dict) or content.get("version") != 1:
        raise ValueError("Invalid experience metadata version")
    for snapshot in [content.get("current"), content.get("pending", content.get("current"))]:
        if (
            not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("source_sha256"), str)
            or not isinstance(snapshot.get("entries"), list)
        ):
            raise ValueError("Invalid experience metadata snapshot")
    return content


def _keep_matching_entries(entries: list[dict], body_hashes: list[str]) -> list[dict]:
    """Match a deletion only when the surviving ordered IDs are unambiguous."""

    def match_positions(hashes, indices):
        remaining = iter(indices)
        positions = []
        for digest in hashes:
            for index in remaining:
                if entries[index]["sha256"] == digest:
                    positions.append(index)
                    break
            else:
                raise ValueError("Cannot identify which experience items were deleted")
        return positions

    first = match_positions(body_hashes, range(len(entries)))
    last = match_positions(reversed(body_hashes), reversed(range(len(entries))))
    if first != last[::-1]:
        raise ValueError("Cannot identify which identical experience item was deleted")
    return [entries[index] for index in first]


def read_document(path: str, absolute: Path, source: str) -> dict[str, MemoryUnit]:
    """Read the experience document with its metadata file."""
    if path != DOCUMENT_PATH:
        raise ValueError("Invalid experience document path")
    sidecar = _read_sidecar(absolute)
    if sidecar is None:
        if not source.strip():
            return {}
        raise ValueError("Experience document metadata is missing")
    current = sidecar["current"]
    pending = sidecar.get("pending")
    fingerprint = _digest(source)
    if pending is not None and current["source_sha256"] != fingerprint:
        if pending["source_sha256"] != fingerprint:
            raise ValueError("Experience document changed during an incomplete write")
        current = pending

    bodies = _split_bodies(source)
    entries = current["entries"]
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
            raise ValueError("Invalid experience metadata entry")
    body_hashes = [_digest(body) for body in bodies]
    if len(bodies) < len(entries):
        entries = _keep_matching_entries(entries, body_hashes)
    elif len(bodies) > len(entries):
        raise ValueError("Use the memory API to add experience items")
    positions = defaultdict(set)
    for index, entry in enumerate(entries):
        positions[entry["sha256"]].add(index)

    units: dict[str, MemoryUnit] = {}
    scope = None
    for index, (entry, body) in enumerate(zip(entries, bodies, strict=True)):
        previous_positions = positions.get(body_hashes[index])
        if (
            fingerprint != current["source_sha256"]
            and previous_positions
            and index not in previous_positions
        ):
            raise ValueError("Use the memory API to reorder experience items")
        unit = loads(json.dumps(entry.get("metadata")).encode("utf-8"))
        if not unit or not unit.segments:
            raise ValueError("Invalid experience unit in metadata")
        if scope is None:
            scope = Scope(org=unit.scope.org, space=unit.scope.space)
        if (
            unit.scope != scope
            or unit.id in units
            or unit.system_metadata.get("memory_type") != "experience"
        ):
            raise ValueError("Invalid experience scope, type or duplicate ID in metadata")
        unit.segments[0] = replace(unit.segments[0], content=body)
        unit.system_metadata["md_filename"] = path
        units[unit.id] = unit
    return units


def _replace_text(path: Path, text: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def refresh_metadata(absolute: Path, source: str, units: Iterable[MemoryUnit]) -> None:
    """Record an indexed Markdown edit without rewriting the user's source."""
    sidecar = _read_sidecar(absolute)
    if sidecar is None or absolute.read_text(encoding="utf-8") != source:
        return
    if "pending" not in sidecar and sidecar["current"]["source_sha256"] == _digest(source):
        return
    state = {"version": 1, "current": _snapshot(source, units)}
    _replace_text(absolute.with_name(METADATA_NAME), json.dumps(state, ensure_ascii=False))


def write_document(
    absolute: Path, source: str, before: Iterable[MemoryUnit], after: Iterable[MemoryUnit],
) -> None:
    """Keep both metadata versions until the Markdown replacement commits."""
    metadata = absolute.with_name(METADATA_NAME)
    if metadata.is_symlink():
        raise ValueError("Experience metadata cannot be a symlink")
    after = list(after)
    document = "".join("- " + _body(unit).replace("\n", "\n  ") + "\n\n" for unit in after)
    updated = _snapshot(document, after)
    if document != source:
        pending = {"version": 1, "current": _snapshot(source, before), "pending": updated}
        _replace_text(metadata, json.dumps(pending, ensure_ascii=False))
        _replace_text(absolute, document)
    try:
        _replace_text(metadata, json.dumps({"version": 1, "current": updated}, ensure_ascii=False))
    except OSError:
        if document == source:
            raise
        # The pending metadata already matches the replaced Markdown after a failed final write.
        logger.warning("Experience metadata cleanup deferred after document replacement")
