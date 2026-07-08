# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Multi-memory markdown store for FileMemoryIndex.

Stores multiple memories per file under ``{root_dir}/memories/{user_id}/{scope_id}/{Type}.md``.
Each file contains one or more frontmatter-delimited memory blocks.

``StorageCodec`` encrypts/decrypts each block's body text individually;
frontmatter YAML always stays readable.

.. warning::

    Single-process use only. Per-path asyncio.Lock is used for in-process
    safety but NO cross-process locking is performed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from jiuwen_memory.foundation.store.base_memory_index import StorageCodec
from jiuwen_memory.foundation.store.index.file_index._chunk_parser import (
    Block,
    blocks_to_markdown,
    find_block_by_id,
    parse_blocks,
)

if TYPE_CHECKING:
    pass


# ------------------------------------------------------------------
# path segment validation — shared by MarkdownStore & FileMemoryIndex
# ------------------------------------------------------------------

def _validate_path_segment(segment: str, *, name: str = "segment") -> str:
    """Validate a single path segment is safe for filesystem use.

    Returns *segment* unchanged on success.

    Raises :class:`ValueError` when the segment contains dangerous patterns
    (path traversal like ``..``, path separators ``/`` or ``\\``, null bytes,
    or is empty / exceeds 128 chars).

    Imported by :class:`FileMemoryIndex` so every path-construction site
    shares the same guard.
    """
    if not segment:
        raise ValueError(f"{name} must not be empty")
    if len(segment) > 128:
        raise ValueError(f"{name} exceeds 128 characters")
    if "\x00" in segment:
        raise ValueError(f"{name} contains null byte")
    if "/" in segment or "\\" in segment:
        raise ValueError(f"{name} must not contain path separators: {segment!r}")
    if segment in (".", ".."):
        raise ValueError(f"{name} must not be {segment!r}")
    return segment


class MarkdownStore:
    """Per-type multi-memory markdown store with YAML frontmatter.

    Directory layout::

        {root_dir}/
        └── memories/
            └── {user_id}/
                └── {scope_id}/
                    ├── user_profile.md
                    ├── episodic_memory.md
                    └── ...

    Each ``{Type}.md`` may contain multiple ``---`` delimited frontmatter blocks.
    """

    _MEMORIES_DIR = "memories"

    def __init__(self, root_dir: str, codec: StorageCodec | None = None):
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / self._MEMORIES_DIR).mkdir(parents=True, exist_ok=True)
        self._codec: StorageCodec | None = codec
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # codec
    # ------------------------------------------------------------------

    def set_codec(self, codec: StorageCodec) -> None:
        self._codec = codec

    def _encode_body(self, text: str) -> str:
        if self._codec is None or not text:
            return text
        return self._codec.encode(text)

    def _decode_body(self, text: str) -> str:
        if self._codec is None or not text:
            return text
        return self._codec.decode(text)

    # ------------------------------------------------------------------
    # path helpers
    # ------------------------------------------------------------------

    def _type_path(self, user_id: str, scope_id: str, type_name: str) -> Path:
        """Return the file path for a given memory type.

        Validates all three segments against path-traversal attacks and
        ensures the resolved absolute path stays within ``self._root``.
        """
        _validate_path_segment(user_id, name="user_id")
        _validate_path_segment(scope_id, name="scope_id")
        _validate_path_segment(type_name, name="type_name")
        rel = Path(self._MEMORIES_DIR) / user_id / scope_id / f"{type_name}.md"
        abs_path = (self._root / rel).resolve()
        root_resolved = self._root.resolve()
        if os.path.commonpath([root_resolved, abs_path]) != str(root_resolved):
            raise ValueError(
                f"Path traversal detected: {abs_path} is outside of {root_resolved}"
            )
        return abs_path

    @staticmethod
    def _type_name_from_path(path: Path) -> str:
        """Extract the type name from a file path (e.g. ``.../user_profile.md`` → ``user_profile``)."""
        return path.stem

    # ------------------------------------------------------------------
    # lock
    # ------------------------------------------------------------------

    async def _get_lock(self, path: Path) -> asyncio.Lock:
        key = str(path)
        if key in self._locks:
            return self._locks[key]
        async with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
        return self._locks[key]

    # ------------------------------------------------------------------
    # raw file I/O
    # ------------------------------------------------------------------

    async def read_file(self, path: Path) -> str | None:
        """Read raw file contents. Returns ``None`` if file does not exist."""
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    async def write_file(self, path: Path, content: str) -> None:
        """Atomically write *content* to *path*. Creates parent directories.

        写临时文件 ``{path}.tmp``，写完后用 ``os.replace`` 原子换入。
        ``os.replace`` 在 POSIX 是 rename(2)、Windows 是 MoveFileEx
        (MOVEFILE_REPLACE_EXISTING)，OS 保证原子：崩溃后 path 要么是完整
        旧文件、要么是完整新文件，不存在半写中间态。

        一个 ``{Type}.md`` 承载该类型全部记忆块，裸 ``write_text`` 的
        ``open('w')`` 在写之前就 O_TRUNC 清空文件，写入中途崩溃（断电/OOM
        kill/磁盘满）会让文件停留在残缺状态，损坏半径是整个类型分区。
        asyncio.Lock 只防并发交错、防不了崩溃，故必须靠 OS 原子替换。
        两者正交：锁防并发，原子替换防崩溃。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = await self._get_lock(path)
        async with lock:
            tmp = path.with_suffix(path.suffix + ".tmp")
            try:
                tmp.write_text(content, encoding="utf-8")
                os.replace(tmp, path)
            except Exception:
                # 清理残留 .tmp：write_text 中途异常或 os.replace 失败时 tmp
                # 可能残留在目录中（write_text 下次会覆盖，但显式清理更干净，
                # 避免权限错误后留下半写 tmp 误导排查）。
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
                raise

    # ------------------------------------------------------------------
    # block-level I/O (with codec)
    # ------------------------------------------------------------------

    async def read_blocks(self, path: Path) -> list[Block]:
        """Read and parse a ``{Type}.md`` file, decoding each block's body."""
        content = await self.read_file(path)
        if content is None:
            return []
        blocks = parse_blocks(content)
        for b in blocks:
            b.text = self._decode_body(b.text)
        return blocks

    async def write_blocks(self, path: Path, blocks: list[Block]) -> None:
        """Encode each block's body and serialize to a ``{Type}.md`` file.

        ``blocks_to_markdown`` recalculates ``start_line``/``end_line`` on the
        encoded blocks in-place, so the returned blocks carry accurate line
        numbers for the on-disk content.
        """
        # Encode bodies before serialization. Line numbers are recomputed by
        # blocks_to_markdown, so we don't carry over the caller's (possibly
        # stale) start_line/end_line.
        encoded_blocks = [
            Block(
                mem_id=b.mem_id,
                type=b.type,
                text=self._encode_body(b.text),
                start_line=0,
                end_line=0,
                fields=b.fields,
                timestamp=b.timestamp,
                hash=b.hash,
            )
            for b in blocks
        ]
        content = blocks_to_markdown(encoded_blocks)
        await self.write_file(path, content)
        # Propagate the recalculated line numbers back to the caller's blocks
        # so callers that re-use the list (e.g. sync_file) see consistent values.
        for src, enc in zip(blocks, encoded_blocks):
            src.start_line = enc.start_line
            src.end_line = enc.end_line

    async def read_block(self, path: Path, mem_id: str) -> Block | None:
        """Read a specific block by *mem_id* from a ``{Type}.md`` file."""
        blocks = await self.read_blocks(path)
        return find_block_by_id(blocks, mem_id)

    # ------------------------------------------------------------------
    # directory listing
    # ------------------------------------------------------------------

    def _scope_dir(self, user_id: str, scope_id: str) -> Path:
        """Return and validate the directory path for a user/scope pair.

        Same validation contract as :meth:`_type_path`: segments are checked
        for traversal characters and the resolved path must stay within
        ``self._root``.
        """
        _validate_path_segment(user_id, name="user_id")
        _validate_path_segment(scope_id, name="scope_id")
        rel = Path(self._MEMORIES_DIR) / user_id / scope_id
        abs_path = (self._root / rel).resolve()
        root_resolved = self._root.resolve()
        if os.path.commonpath([root_resolved, abs_path]) != str(root_resolved):
            raise ValueError(
                f"Path traversal detected: {abs_path} is outside of {root_resolved}"
            )
        return abs_path

    def _user_dir(self, user_id: str) -> Path:
        """Return and validate the directory path for a user.

        Same validation as :meth:`_scope_dir` but only checks *user_id*.
        """
        _validate_path_segment(user_id, name="user_id")
        rel = Path(self._MEMORIES_DIR) / user_id
        abs_path = (self._root / rel).resolve()
        root_resolved = self._root.resolve()
        if os.path.commonpath([root_resolved, abs_path]) != str(root_resolved):
            raise ValueError(
                f"Path traversal detected: {abs_path} is outside of {root_resolved}"
            )
        return abs_path

    def list_type_files(self, user_id: str, scope_id: str) -> list[Path]:
        """List all ``{Type}.md`` files under a user/scope directory."""
        scope_dir = self._scope_dir(user_id, scope_id)
        if not scope_dir.exists():
            return []
        return sorted(p for p in scope_dir.glob("*.md") if p.is_file())

    def list_all_mem_ids(self, user_id: str, scope_id: str) -> list[str]:
        """List all memory IDs across all type files in a user/scope.

        Reads every ``{Type}.md`` file to extract block IDs.
        Used by ``list_memories`` when DB may be stale.
        """
        ids: list[str] = []
        for path in self.list_type_files(user_id, scope_id):
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for block in parse_blocks(content):
                ids.append(block.mem_id)
        return ids

    def list_user_scopes(self) -> list[tuple[str, str]]:
        """List all (user_id, scope_id) pairs by scanning directories."""
        scopes: set[tuple[str, str]] = set()
        users_dir = self._root / self._MEMORIES_DIR
        if not users_dir.exists():
            return []
        for user_dir in users_dir.iterdir():
            if not user_dir.is_dir():
                continue
            for scope_dir in user_dir.iterdir():
                if scope_dir.is_dir():
                    scopes.add((user_dir.name, scope_dir.name))
        return list(scopes)

    # ------------------------------------------------------------------
    # directory deletion
    # ------------------------------------------------------------------

    def delete_scope_dir(self, user_id: str, scope_id: str) -> None:
        scope_dir = self._scope_dir(user_id, scope_id)
        if scope_dir.exists():
            shutil.rmtree(scope_dir, ignore_errors=True)

    def delete_user_dir(self, user_id: str) -> None:
        user_dir = self._user_dir(user_id)
        if user_dir.exists():
            shutil.rmtree(user_dir, ignore_errors=True)

    def delete_scope_all(self, scope_id: str) -> None:
        _validate_path_segment(scope_id, name="scope_id")
        users_dir = self._root / self._MEMORIES_DIR
        if not users_dir.exists():
            return
        for user_dir in users_dir.iterdir():
            if not user_dir.is_dir():
                continue
            scope_dir = user_dir / scope_id
            if scope_dir.exists():
                shutil.rmtree(scope_dir, ignore_errors=True)
