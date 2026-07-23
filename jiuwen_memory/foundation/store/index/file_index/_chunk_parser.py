# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Multi-block frontmatter parser for FileMemoryIndex.

Parses a single ``{Type}.md`` file that contains multiple memories delimited by
``---`` frontmatter blocks. Each block represents one MemoryDoc in the file.

.. code-block:: text

    ---
    id: m1
    type: user_profile
    timestamp: 2026-07-04T14:00:00+0800
    fields:
      source: conversation
    ---
    body text

    ---
    id: m2
    type: user_profile
    ---
    another body
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Block:
    """A single memory's structured representation within a ``{Type}.md`` file.

    Attributes:
        mem_id: Unique memory identifier (from frontmatter ``id``).
        type: Memory type, must match the file name stem.
        text: Body content of the memory.
        start_line: 1-based line number of the opening ``---``.
        end_line: 1-based line number of the last body line.
        fields: Optional extension fields.
        timestamp: ISO 8601 timestamp string.
        blacklisted: Whether this memory is forgotten (Ebbinghaus).
        is_important: Whether this memory is protected from forgetting.
        hash: SHA256 first-16-chars of ``text``.
    """

    mem_id: str
    type: str
    text: str
    start_line: int = 0
    end_line: int = 0
    fields: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    blacklisted: bool = False
    is_important: bool = False
    hash: str = ""

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = hash_text(self.text)


def hash_text(text: str) -> str:
    """SHA256 first 16 hex chars of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_blocks(content: str) -> list[Block]:
    """Parse a ``{Type}.md`` file into a list of :class:`Block` objects.

    The file may contain multiple frontmatter-delimited memories::

        ---
        id: ...
        ---
        body

        ---
        id: ...
        ---
        body

    Returns blocks in file order.  Empty/whitespace-only files return ``[]``.
    """
    if not content or not content.strip():
        return []

    lines = content.split("\n")
    blocks: list[Block] = []
    i = 0
    n = len(lines)

    while i < n:
        # Find opening ---
        if lines[i].strip() != "---":
            i += 1
            continue

        fm_start = i  # 0-based index of opening ---
        i += 1

        # Collect frontmatter lines until closing ---
        fm_lines: list[str] = []
        fm_end: int | None = None
        while i < n:
            if lines[i].strip() == "---":
                fm_end = i  # 0-based index of closing ---
                i += 1
                break
            fm_lines.append(lines[i])
            i += 1

        if fm_end is None:
            # Unclosed frontmatter: treat the rest of the file as this block's
            # body so we don't silently drop user edits. fm_lines already holds
            # whatever YAML-like content was collected.
            body_lines: list[str] = []
            body_start = i
            while i < n:
                body_lines.append(lines[i])
                i += 1
        else:
            # Skip leading blank lines after closing ---
            while i < n and lines[i].strip() == "":
                i += 1

            # Collect body lines until next --- or EOF
            body_lines = []
            body_start = i
            while i < n:
                if lines[i].strip() == "---":
                    break
                body_lines.append(lines[i])
                i += 1

        # Strip trailing blank lines from body (affects text AND end_line).
        while body_lines and body_lines[-1].strip() == "":
            body_lines.pop()

        # end_line = the line number (1-based) of the last content line of the
        # body. body_start is 0-based index of the first body line collected;
        # len(body_lines) counts content lines after trailing-blank stripping.
        # If body is empty, end_line falls back to one before body_start.
        body_end = body_start + len(body_lines) - 1 if body_lines else body_start - 1

        # Parse frontmatter YAML. If it fails to parse (e.g. an unclosed
        # frontmatter swallowed body lines that aren't valid YAML), fall back
        # to a line-by-line scan for ``id``/``type`` keys so we still recover
        # the block identity instead of dropping the whole block. Lines that
        # don't look like frontmatter keys are recovered into the body.
        import yaml

        fm_text = "\n".join(fm_lines)
        recovered_body_lines: list[str] = []
        try:
            fm = yaml.safe_load(fm_text) or {}
        except yaml.YAMLError:
            fm = {}
        if not isinstance(fm, dict):
            fm = {}

        mem_id = str(fm.get("id", ""))
        mem_type = str(fm.get("type", ""))
        if not mem_id:
            # Fallback for malformed/unclosed frontmatter: scan fm_lines for
            # recognizable keys, and treat the rest as body content so the
            # block is kept (with its text) rather than silently dropped.
            _known_keys = ("id", "type", "timestamp", "fields", "user_id", "scope_id")
            for raw in fm_lines:
                stripped = raw.strip()
                low = stripped.lower()
                if ":" in stripped and low.split(":", 1)[0].strip() in _known_keys:
                    key, _, val = stripped.partition(":")
                    key = key.strip().lower()
                    val = val.strip().strip("\"'")
                    if key == "id" and not mem_id:
                        mem_id = val
                    elif key == "type" and not mem_type:
                        mem_type = val
                elif stripped:
                    recovered_body_lines.append(raw)
            if recovered_body_lines:
                body_lines = recovered_body_lines
                # recovered_body_lines 来自 fm_lines（源行号从 fm_start+1 开始），
                # 而非原始 body_start（未闭合路径下 body_start=n=EOF）。
                # 重置 body_start 使 end_line 与实际内容位置对齐。
                body_start = fm_start + 1
                body_end = body_start + len(body_lines) - 1
        if not mem_id:
            # Block without id — skip
            continue

        body = "\n".join(body_lines)
        # Strip trailing blank line if present
        if body.endswith("\n"):
            body = body[:-1]

        ts_raw = fm.get("timestamp")
        timestamp: str
        if isinstance(ts_raw, datetime):
            timestamp = ts_raw.strftime("%Y-%m-%dT%H:%M:%S%z")
        elif isinstance(ts_raw, str) and ts_raw:
            timestamp = ts_raw
        else:
            timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

        fields = fm.get("fields", {}) or {}

        # Forgetting-pipeline flags. Persisted as top-level frontmatter keys
        # (parallel to id/type/timestamp) so list_memories can render them
        # to a SQL WHERE clause on the chunks table, and search/list can
        # apply the default NE("blacklisted", True) filter the same way
        # SimpleMemoryIndex does on its vector collection schema.
        blacklisted = bool(fm.get("blacklisted", False))
        is_important = bool(fm.get("is_important", False))

        blocks.append(Block(
            mem_id=mem_id,
            type=mem_type,
            text=body,
            start_line=fm_start + 1,   # 1-based
            end_line=body_end + 1,      # 1-based
            fields=fields,
            timestamp=timestamp,
            blacklisted=blacklisted,
            is_important=is_important,
        ))

    return blocks


def blocks_to_markdown(blocks: list[Block]) -> str:
    """Serialize a list of :class:`Block` objects to a ``{Type}.md`` file string.

    Each block is rendered as::

        ---
        id: ...
        type: ...
        timestamp: ...
        fields: ...
        ---
        body text

    A trailing newline is always appended.

    .. important::

        This function **recalculates** ``start_line``/``end_line`` on each
        block in-place as it serializes, so the returned blocks always carry
        accurate line numbers for the generated text. Callers must serialize
        via this function (not ad-hoc string building) to keep line numbers
        consistent with on-disk content.
    """
    import yaml

    parts: list[str] = []
    current_line = 1  # 1-based line cursor as we append blocks
    for block in blocks:
        fm: dict[str, Any] = {
            "id": block.mem_id,
            "type": block.type,
            "timestamp": block.timestamp,
        }
        if block.blacklisted:
            fm["blacklisted"] = True
        if block.is_important:
            fm["is_important"] = True
        if block.fields:
            fm["fields"] = block.fields

        fm_yaml = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
        if block.text:
            chunk = f"---\n{fm_yaml}\n---\n\n{block.text}\n"
        else:
            chunk = f"---\n{fm_yaml}\n---\n"

        # Compute accurate line numbers for this block within the full file.
        chunk_lines = chunk.split("\n")
        # start_line: the opening '---' (1-based, absolute in file)
        block.start_line = current_line
        # end_line: the last line of the body (or the closing '---' if no body).
        # chunk always ends with a trailing newline, so the last element after
        # split is '' (the empty string after the final '\n'); the actual last
        # content line is chunk_lines[-2].
        block.end_line = current_line + len(chunk_lines) - 2

        parts.append(chunk)
        # Advance cursor past this block. Two blocks are separated by a blank
        # line in the joined output ("\n".join), which consumes one line.
        current_line = block.end_line + 2  # +1 for trailing newline, +1 for separator blank line

    return "\n".join(parts) + "\n" if parts else ""


def find_block_by_id(blocks: list[Block], mem_id: str) -> Block | None:
    """Return the block with *mem_id* or ``None``."""
    for b in blocks:
        if b.mem_id == mem_id:
            return b
    return None


def remove_blocks_by_ids(blocks: list[Block], ids_to_remove: set[str]) -> list[Block]:
    """Return a new list with blocks whose mem_id is in *ids_to_remove* filtered out."""
    return [b for b in blocks if b.mem_id not in ids_to_remove]


def merge_blocks(
    existing: list[Block],
    new_blocks: list[Block],
) -> list[Block]:
    """Merge *new_blocks* into *existing*.

    - Same ``mem_id`` → replace the existing block.
    - New ``mem_id`` → append at the end.

    Returns a new list; does not mutate inputs.
    """
    by_id = {b.mem_id: b for b in existing}
    for nb in new_blocks:
        by_id[nb.mem_id] = nb
    # Preserve order: existing first, then new ones appended
    result: list[Block] = []
    seen: set[str] = set()
    for b in existing:
        if b.mem_id not in seen:
            result.append(by_id[b.mem_id])
            seen.add(b.mem_id)
    for nb in new_blocks:
        if nb.mem_id not in seen:
            result.append(nb)
            seen.add(nb.mem_id)
    return result
