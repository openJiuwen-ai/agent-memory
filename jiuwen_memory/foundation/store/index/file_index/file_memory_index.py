# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
FileMemoryIndex — markdown + SQLite 记忆文件系统（per-type 多记忆存储 + 增量重索引）。

特性：

- **按 type 分文件**：同 type 的记忆合并到一个 ``{Type}.md`` under
  ``memories/{user_id}/{scope_id}/``。
- **Watchdog 监听**：检测外部 .md 编辑并自动增量同步。
- **增量重索引**：只有变化的 block 才重新 embedding，行号漂移走廉价 UPDATE。
- **``files`` 表**：按文件 hash 做脏检查，快速定位需同步的文件。

Implements :class:`BaseMemoryIndex`。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

from jiuwen_memory.common.exception.codes import StatusCode
from jiuwen_memory.common.exception.errors import build_error
from jiuwen_memory.common.logging import memory_logger
from jiuwen_memory.common.logging.events import LogEventType
from jiuwen_memory.foundation.store.base_memory_index import (
    BaseMemoryIndex,
    MemoryDoc,
    StorageCodec,
)
from jiuwen_memory.foundation.store.filter_dsl import FilterGroup, FilterLogic
from jiuwen_memory.foundation.store.index.file_index._chunk_parser import (
    Block,
    blocks_to_markdown,
    find_block_by_id,
    merge_blocks,
    parse_blocks,
    remove_blocks_by_ids,
)
from jiuwen_memory.foundation.store.index.file_index._file_watcher import MemoryFileWatcher
from jiuwen_memory.foundation.store.index.file_index._md_store import MarkdownStore, _validate_path_segment
from jiuwen_memory.foundation.store.index.file_index._vector_index import (
    SearchConstraints,
    TenantScope,
    VectorIndex,
)


class FileMemoryIndex(BaseMemoryIndex):
    """Markdown + SQLite vector index with per-type files and incremental reindex.

    Usage::

        index = FileMemoryIndex(
            root_dir="./data",
            embedding_model=FakeEmbedding(dim=128),
        )
        await index.add_memories("user1", "scope1", [doc1, doc2])
        results = await index.search("user1", "scope1", "hello world")
    """

    def __init__(self, root_dir: str, embedding_model: Any = None, codec: StorageCodec | None = None):
        self._root_dir = Path(root_dir)
        self._root_dir.mkdir(parents=True, exist_ok=True)
        (self._root_dir / "memories").mkdir(parents=True, exist_ok=True)
        self._embedding_model = embedding_model
        self._codec: StorageCodec | None = codec

        # sub-modules
        self._md_store = MarkdownStore(root_dir=root_dir, codec=codec)
        self._vec_index = VectorIndex(
            db_path=str(self._root_dir / "memory.db"),
            embedding_model=embedding_model,
            codec=codec,
        )

        # Watchdog
        self._watcher = MemoryFileWatcher(self._root_dir, self._on_file_changed)

        # Per-path sync locks (concurrency safety)
        self._sync_locks: dict[str, asyncio.Lock] = {}
        self._sync_locks_guard = asyncio.Lock()

        # Schema version & backups
        self._schema_version = 0
        self._backups: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------

    def set_embedding_model(self, embedding_model: Any) -> None:
        self._embedding_model = embedding_model
        self._vec_index.set_embedding_model(embedding_model)

    def set_storage_codec(self, codec: StorageCodec) -> None:
        self._codec = codec
        self._md_store.set_codec(codec)

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    @property
    def vec_index(self) -> VectorIndex:
        return self._vec_index

    @property
    def embedding_model(self) -> Any:
        return self._embedding_model

    @property
    def md_store(self) -> MarkdownStore:
        return self._md_store

    @property
    def watcher(self) -> MemoryFileWatcher:
        return self._watcher

    # ------------------------------------------------------------------
    # watchdog
    # ------------------------------------------------------------------

    def start_watcher(self) -> None:
        """Start the watchdog file watcher. Idempotent — safe to call twice.

        Call after the event loop is running. In test scenarios where
        the event loop is created with ``asyncio.run()``, this should be
        called inside an async function.

        Repeated calls when the watcher is already running are no-ops: the
        underlying ``MemoryFileWatcher.start()`` would otherwise create a
        second ``Observer`` and overwrite the first's reference, leaking
        the first Observer's background thread. Guarding here lets callers
        (e.g. ``register_store`` + ``file_memory_server`` startup both
        wiring it) call defensively without leaking.
        """
        if self._watcher.running:
            return
        self._watcher.start()

    def stop_watcher(self) -> None:
        """Stop the watchdog file watcher."""
        self._watcher.stop()

    async def _on_file_changed(self, abs_path: str, event_type: str) -> None:
        """Callback from watchdog — sync a changed .md file."""
        rel_path = self._abs_to_rel(abs_path)
        if event_type == "deleted":
            # File was deleted → remove all chunks for this path
            await self._handle_deleted_file(rel_path)
            return
        # created / modified → re-sync
        await self._sync_file(rel_path)

    # ------------------------------------------------------------------
    # path helpers
    # ------------------------------------------------------------------

    def _rel_path(self, user_id: str, scope_id: str, type_name: str) -> str:
        """Relative path for a type file (stored in DB).

        Validates all three segments against path-traversal attacks before
        constructing the path.
        """
        _validate_path_segment(user_id, name="user_id")
        _validate_path_segment(scope_id, name="scope_id")
        _validate_path_segment(type_name, name="type_name")
        return f"memories/{user_id}/{scope_id}/{type_name}.md"

    def _abs_path(self, user_id: str, scope_id: str, type_name: str) -> Path:
        """Absolute path for a type file.

        Resolves and verifies the path stays within ``_root_dir`` to
        prevent path-traversal writes.
        """
        rel = self._rel_path(user_id, scope_id, type_name)
        abs_path = (self._root_dir / rel).resolve()
        root_resolved = self._root_dir.resolve()
        if os.path.commonpath([root_resolved, abs_path]) != str(root_resolved):
            raise ValueError(
                f"Path traversal detected: {abs_path} is outside of {root_resolved}"
            )
        return abs_path

    def _abs_to_rel(self, abs_path: str) -> str:
        """Convert an absolute path to a root_dir-relative path."""
        return str(Path(abs_path).relative_to(self._root_dir))

    def _parse_rel_path(self, rel_path: str) -> tuple[str, str, str]:
        """Parse ``memories/{uid}/{sid}/{Type}.md`` → (user_id, scope_id, type_name).

        Validates the parsed components so that paths from DB/filesystem are
        also checked before being used in further operations.
        """
        parts = rel_path.replace("\\", "/").split("/")
        # parts = ["memories", uid, sid, "Type.md"]
        uid = parts[1] if len(parts) > 1 else ""
        sid = parts[2] if len(parts) > 2 else ""
        tname = Path(parts[3]).stem if len(parts) > 3 else ""
        _validate_path_segment(uid, name="user_id (parsed)")
        _validate_path_segment(sid, name="scope_id (parsed)")
        _validate_path_segment(tname, name="type_name (parsed)")
        return uid, sid, tname

    # ------------------------------------------------------------------
    # sync lock
    # ------------------------------------------------------------------

    async def _get_sync_lock(self, path: str) -> asyncio.Lock:
        if path in self._sync_locks:
            return self._sync_locks[path]
        async with self._sync_locks_guard:
            if path not in self._sync_locks:
                self._sync_locks[path] = asyncio.Lock()
        return self._sync_locks[path]

    # ------------------------------------------------------------------
    # core sync
    # ------------------------------------------------------------------

    async def _sync_file(self, rel_path: str) -> None:
        """Incremental sync for a single ``{Type}.md`` file (watchdog path).

        Public entry point for the watchdog callback — acquires the per-file
        sync lock so it can't interleave with concurrent public writes
        (``add_memories`` / ``delete_memories``) on the same file.
        """
        lock = await self._get_sync_lock(rel_path)
        async with lock:
            await self._sync_file_unlocked(rel_path)

    async def _sync_file_unlocked(self, rel_path: str) -> None:
        """Actual sync logic — caller MUST already hold ``_get_sync_lock(rel_path)``.

        Split out from :meth:`_sync_file` so that public write methods, which
        already hold the per-file lock around their read→modify→write→sync
        span, can invoke the sync step without re-acquiring the (non-reentrant)
        ``asyncio.Lock`` — which would otherwise deadlock.
        """
        uid, sid, tname = self._parse_rel_path(rel_path)
        abs_path = self._root_dir / rel_path
        if not abs_path.exists():
            await self._handle_deleted_file(rel_path)
            return
        content = abs_path.read_text(encoding="utf-8")
        # Read decoded blocks (codec applied) for correct embedding
        decoded_blocks = await self._md_store.read_blocks(abs_path)
        await self._vec_index.sync_file(
            path=rel_path,
            tenant=TenantScope(user_id=uid, scope_id=sid),
            type_name=tname,
            file_content=content,
            blocks=decoded_blocks,
        )

    async def _handle_deleted_file(self, rel_path: str) -> None:
        """Remove all chunks and the files table entry for a deleted .md file."""
        try:
            await self._vec_index.delete_by_path(rel_path)
        except sqlite3.Error:
            memory_logger.warning(
                "Failed to delete chunks for removed file — will retry on next sync",
                event_type=LogEventType.STORE_DELETE,
                file_path=rel_path,
                exc_info=True,
            )

    async def _ensure_synced(self, user_id: str, scope_id: str) -> None:
        """Ensure all type files under a user/scope are synced before search.

        Compares file hashes against the ``files`` table; only syncs files
        with mismatched hashes.
        """
        type_files = self._md_store.list_type_files(user_id, scope_id)
        for abs_path in type_files:
            rel_path = self._abs_to_rel(str(abs_path))
            try:
                content = abs_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            current_hash = VectorIndex.file_hash(content)
            stored_hash = self._vec_index.get_file_hash_from_db(rel_path)
            if current_hash != stored_hash:
                # Read decoded blocks for correct embedding
                decoded_blocks = await self._md_store.read_blocks(abs_path)
                await self._vec_index.sync_file(
                    path=rel_path,
                    tenant=TenantScope(user_id=user_id, scope_id=scope_id),
                    type_name=abs_path.stem,
                    file_content=content,
                    blocks=decoded_blocks,
                )

    # ------------------------------------------------------------------
    # block ↔ MemoryDoc conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _block_to_doc(block: Block) -> MemoryDoc:
        """Convert a :class:`Block` to a :class:`MemoryDoc`."""
        from datetime import datetime, timezone
        ts: datetime
        if block.timestamp:
            try:
                ts = datetime.fromisoformat(block.timestamp)
            except ValueError:
                ts = datetime.now(timezone.utc).astimezone()
        else:
            ts = datetime.now(timezone.utc).astimezone()
        return MemoryDoc(
            id=block.mem_id,
            text=block.text,
            type=block.type,
            timestamp=ts,
            fields=block.fields,
            blacklisted=block.blacklisted,
            is_important=block.is_important,
        )

    @staticmethod
    def _doc_to_block(doc: MemoryDoc) -> Block:
        """Convert a :class:`MemoryDoc` to a :class:`Block`."""
        from jiuwen_memory.foundation.store.index.file_index._chunk_parser import hash_text
        ts = (
            doc.timestamp.strftime("%Y-%m-%dT%H:%M:%S%z")
            if doc.timestamp
            else ""
        )
        return Block(
            mem_id=doc.id,
            type=doc.type,
            text=doc.text,
            start_line=0,  # will be recalculated on write
            end_line=0,
            fields=dict(doc.fields),
            timestamp=ts,
            blacklisted=doc.blacklisted,
            is_important=doc.is_important,
        )

    # ------------------------------------------------------------------
    # BaseMemoryIndex implementation
    # ------------------------------------------------------------------

    async def add_memories(self, user_id: str, scope_id: str, memories: list[MemoryDoc]) -> None:
        """Add memories — grouped by type into ``{Type}.md`` files."""
        if not memories:
            return
        if not self._embedding_model:
            memory_logger.error(
                "Embedding model not initialized.",
                event_type=LogEventType.MEMORY_STORE,
                scope_id=scope_id,
            )
            raise build_error(
                StatusCode.MEMORY_ADD_MEMORY_EXECUTION_ERROR,
                memory_type="vector store",
                error_msg="embedding model not initialized",
            )

        # Group by type
        by_type: dict[str, list[MemoryDoc]] = {}
        for doc in memories:
            by_type.setdefault(doc.type, []).append(doc)

        for type_name, docs in by_type.items():
            rel_path = self._rel_path(user_id, scope_id, type_name)
            abs_path = self._abs_path(user_id, scope_id, type_name)

            # Hold the per-file lock across the whole read→merge→write→sync span.
            # Without it, two concurrent add_memories on the same {Type}.md would
            # each read a stale snapshot and the later write would silently drop
            # the earlier's new blocks (and the subsequent sync would purge them
            # from the SQLite index too). The lock is per rel_path, so writes to
            # different files (different user/scope/type) still proceed in parallel.
            lock = await self._get_sync_lock(rel_path)
            async with lock:
                await self._add_locked(
                    rel_path, abs_path,
                    TenantScope(user_id=user_id, scope_id=scope_id),
                    type_name, docs,
                )

    async def _add_locked(
        self,
        rel_path: str,
        abs_path: Path,
        tenant: TenantScope,
        type_name: str,
        docs: list[MemoryDoc],
    ) -> None:
        """Add ``docs`` to ``{Type}.md`` — caller MUST hold ``_get_sync_lock(rel_path)``.

        抽自 add_memories 的锁内逻辑（read→merge→write→sync），不加锁，供
        update_memories 在外层同一把锁内调用，避免 delete+add 两次加锁间的
        无锁空档（中间态外泄 + 丢写窗口）。参照已有的 _sync_file_unlocked 模式。
        ``user_id``/``scope_id`` 封装为 ``TenantScope`` 减少参数个数（G.FNM.03）。
        """
        # Read existing blocks (decode body via md_store)
        existing_blocks = await self._md_store.read_blocks(abs_path)

        # Merge new docs
        new_blocks = [self._doc_to_block(d) for d in docs]
        merged = merge_blocks(existing_blocks, new_blocks)

        # Write back (encode body via md_store)
        await self._md_store.write_blocks(abs_path, merged)

        # Sync to vector index — pass decoded blocks for correct embedding.
        # Call vec_index.sync_file directly (NOT _sync_file, which would
        # re-acquire this same non-reentrant lock and deadlock).
        content = abs_path.read_text(encoding="utf-8")
        await self._vec_index.sync_file(
            path=rel_path,
            tenant=tenant,
            type_name=type_name,
            file_content=content,
            blocks=merged,
        )

    async def update_memories(self, user_id: str, scope_id: str, memories: list[MemoryDoc]) -> None:
        """Update memories — atomic delete-then-add under one per-file lock.

        原实现 ``delete_memories + add_memories`` 两次独立加锁，中间有无锁空档：
        delete 完 add 未开始时记忆暂时消失（并发 search 漏召），且并发 add/update
        可插入空档与 add 的整文件重写交错、互相覆盖（丢写窗口，等价于 P0-① 口子）。

        修复：按 rel_path 分组，每文件一把锁，锁内调 ``_delete_locked`` +
        ``_add_locked``（不加锁的内部方法，避免 asyncio.Lock 不可重入死锁），
        delete 与 add 同一 ``async with`` 内不释放，使"删旧加新"对外不可分割。

        跨 type update（旧 type ≠ 新 type）：按 add 的 type 算 rel_path 持锁，
        delete 的旧 path 可能不同 → 旧文件删除不在当前锁内，存在中间态。此场景
        罕见（update 通常不改 type，见 fragment_memory_manager 沿用 old_doc.type），
        且"换 type"本就是非原子语义，可接受。
        """
        if not memories:
            return

        # 按 rel_path（由 type 决定）分组，每文件一把锁
        by_path: dict[str, list[MemoryDoc]] = {}
        for doc in memories:
            rel = self._rel_path(user_id, scope_id, doc.type)
            by_path.setdefault(rel, []).append(doc)

        for rel_path, docs in by_path.items():
            type_name = docs[0].type
            abs_path = self._abs_path(user_id, scope_id, type_name)
            uid, sid, tname = self._parse_rel_path(rel_path)
            ids = {d.id for d in docs}

            lock = await self._get_sync_lock(rel_path)
            async with lock:
                # 锁内 delete+add，中间不放锁——同 path 的"删旧加新"原子
                await self._delete_locked(
                    rel_path, abs_path,
                    TenantScope(user_id=uid, scope_id=sid),
                    tname, ids,
                )
                await self._add_locked(
                    rel_path, abs_path,
                    TenantScope(user_id=user_id, scope_id=scope_id),
                    type_name, docs,
                )


    async def update_mem_by_id(
        self,
        user_id: str,
        scope_id: str,
        mem_id: str,
        fields: dict[str, Any],
    ) -> None:
        """Update scalar fields on a single memory document by id.

        Only the fields present in ``fields`` are modified; the body text and
        its embedding are never touched (no re-embedding roundtrip). The
        ``.md`` file is the source of truth, so scalar fields are written into
        the block's ``fields`` dict (and ``timestamp`` when requested) and the
        block is rewritten in place under the per-file sync lock.

        Because the body hash does not change, the downstream
        :meth:`VectorIndex.sync_file` call classifies the block as
        "text-unchanged" and at most re-lines it — the embedding is preserved.

        Raises ``STORE_VECTOR_DOC_INVALID`` when the memory is not found.
        """
        if not fields:
            return
        if not mem_id:
            raise build_error(
                StatusCode.STORE_VECTOR_DOC_INVALID,
                error_msg="update_mem_by_id requires non-empty mem_id",
            )

        # Locate the file that holds this mem_id (tenant-scoped).
        rel_path = self._vec_index.get_path_for_mem_id_scoped(mem_id, user_id, scope_id)
        abs_path: Path | None = None
        if rel_path:
            abs_path = self._root_dir / rel_path
            if not abs_path.exists():
                abs_path = None

        if abs_path is None:
            # Fallback: scan the scope's type files (covers hand-edited files
            # whose sync to the DB hasn't run yet).
            for type_file in self._md_store.list_type_files(user_id, scope_id):
                block = await self._md_store.read_block(type_file, mem_id)
                if block is not None:
                    abs_path = type_file
                    rel_path = self._abs_to_rel(str(type_file))
                    break

        if abs_path is None:
            raise build_error(
                StatusCode.STORE_VECTOR_DOC_INVALID,
                error_msg=(
                    f"memory doc not found: user_id={user_id} "
                    f"scope_id={scope_id} mem_id={mem_id}"
                ),
            )

        uid, sid, tname = self._parse_rel_path(rel_path)
        tenant = TenantScope(user_id=uid, scope_id=sid)
        lock = await self._get_sync_lock(rel_path)
        async with lock:
            blocks = await self._md_store.read_blocks(abs_path)
            target = find_block_by_id(blocks, mem_id)
            if target is None:
                raise build_error(
                    StatusCode.STORE_VECTOR_DOC_INVALID,
                    error_msg=(
                        f"memory doc not found: user_id={user_id} "
                        f"scope_id={scope_id} mem_id={mem_id}"
                    ),
                )
            # Merge scalar field updates onto the block. ``blacklisted`` /
            # ``is_important`` are top-level Block attributes (persisted to
            # the chunks table + the .md frontmatter top-level keys); any
            # other field falls through to the .md frontmatter ``fields:``
            # map (the single extension surface for arbitrary user fields).
            scalar_updates: dict[str, Any] = {}
            for name, value in fields.items():
                if name == "blacklisted":
                    target.blacklisted = bool(value)
                    scalar_updates["blacklisted"] = bool(value)
                elif name == "is_important":
                    target.is_important = bool(value)
                    scalar_updates["is_important"] = bool(value)
                else:
                    target.fields[name] = value

            await self._md_store.write_blocks(abs_path, blocks)

            content = abs_path.read_text(encoding="utf-8")
            await self._vec_index.sync_file(
                path=rel_path,
                tenant=tenant,
                type_name=tname,
                file_content=content,
                blocks=blocks,
            )

            # ``sync_file`` classifies blocks by body hash — a pure-scalar
            # flip (blacklisted=True with unchanged text) lands in the
            # "unchanged" bucket and is never re-upserted, so the new
            # scalar value would not reach the chunks table. Direct
            # UPDATE keeps the SQL index in lockstep with the .md file
            # the moment the flip happens (parallel to SimpleMemoryIndex's
            # ``vector_store.update_doc_fields`` scalar path).
            if scalar_updates:
                self._vec_index.update_chunk_scalars(mem_id, scalar_updates)


    async def delete_memories(self, user_id: str, scope_id: str, ids: list[str]) -> None:
        """Delete memories by ID — removes blocks from their ``{Type}.md`` files.

        The markdown files are the source of truth, so even ids that the vector
        index doesn't know about (e.g. the user hand-edited a file but sync
        hasn't run yet) must still be removed from disk. We first try to
        locate each id via the DB; for any id not found, we fall back to
        scanning the scope's ``{Type}.md`` files to find which file holds it.
        """
        if not ids:
            return
        ids_set = set(ids)

        # 1. Locate ids via the DB (the common path)
        path_to_ids: dict[str, set[str]] = {}
        unresolved = set[str]()
        for mem_id in ids_set:
            path = self._vec_index.get_path_for_mem_id_scoped(mem_id, user_id, scope_id)
            if path:
                path_to_ids.setdefault(path, set()).add(mem_id)
            else:
                unresolved.add(mem_id)

        # 2. For ids the DB doesn't know about, scan the scope's type files.
        #    This keeps deletes correct when md has diverged from the DB.
        if unresolved:
            for type_file in self._md_store.list_type_files(user_id, scope_id):
                blocks = await self._md_store.read_blocks(type_file)
                rel = self._abs_to_rel(str(type_file))
                for b in blocks:
                    if b.mem_id in unresolved:
                        path_to_ids.setdefault(rel, set()).add(b.mem_id)
                # Drop ids we've resolved so far; we can stop early once all
                # unresolved ids are located, but scanning is cheap either way.
                unresolved -= {b.mem_id for b in blocks}
                if not unresolved:
                    break

        # 3. If some ids still aren't found anywhere, they simply don't exist.
        #    Nothing to delete — which is the correct outcome for a missing id.

        for rel_path, ids_to_remove in path_to_ids.items():
            abs_path = self._root_dir / rel_path
            uid, sid, tname = self._parse_rel_path(rel_path)

            # Hold the per-file lock across read→remove→write→sync; same
            # rationale as add_memories — prevents a concurrent writer's
            # stale snapshot from resurrecting blocks we just removed.
            lock = await self._get_sync_lock(rel_path)
            async with lock:
                await self._delete_locked(
                    rel_path, abs_path,
                    TenantScope(user_id=uid, scope_id=sid),
                    tname, ids_to_remove,
                )

    async def _delete_locked(
        self,
        rel_path: str,
        abs_path: Path,
        tenant: TenantScope,
        type_name: str,
        ids_to_remove: set[str],
    ) -> None:
        """Remove ``ids_to_remove`` from ``{Type}.md`` — caller MUST hold lock.

        抽自 delete_memories 的锁内逻辑（read→remove→write→sync），不加锁，供
        update_memories 在外层同一把锁内调用。返回 early 的情况（文件不存在、
        无内容可删）在锁内直接 return，调用方（update）继续 _add_locked 即可。
        ``user_id``/``scope_id`` 封装为 ``TenantScope`` 减少参数个数（G.FNM.03）。
        """
        # Read existing blocks
        existing_blocks = await self._md_store.read_blocks(abs_path)
        if not existing_blocks:
            return

        # Remove blocks by id
        remaining = remove_blocks_by_ids(existing_blocks, ids_to_remove)

        if len(remaining) == len(existing_blocks):
            return  # nothing removed

        if remaining:
            await self._md_store.write_blocks(abs_path, remaining)
        else:
            # File is empty — delete it entirely
            await self._handle_deleted_file(rel_path)
            try:
                abs_path.unlink()
            except FileNotFoundError:
                pass
            return

        # Sync to vector index — pass decoded remaining blocks.
        # Direct vec_index.sync_file call (NOT _sync_file) to avoid
        # re-acquiring this same lock and deadlocking.
        content = abs_path.read_text(encoding="utf-8")
        await self._vec_index.sync_file(
            path=rel_path,
            tenant=tenant,
            type_name=type_name,
            file_content=content,
            blocks=remaining,
        )

    async def delete_by_user_and_scope(self, user_id: str, scope_id: str) -> None:
        self._md_store.delete_scope_dir(user_id, scope_id)
        await self._vec_index.delete_by_user_and_scope(user_id, scope_id)

    async def delete_by_user(self, user_id: str) -> None:
        self._md_store.delete_user_dir(user_id)
        await self._vec_index.delete_by_user(user_id)

    async def delete_by_scope(self, scope_id: str) -> None:
        self._md_store.delete_scope_all(scope_id)
        await self._vec_index.delete_by_scope(scope_id)

    async def get_by_id(self, user_id: str, scope_id: str, mem_id: str) -> MemoryDoc | None:

        # First, try to find the path from chunks table — scoped to the caller's
        # tenant so a cross-tenant id can't resolve to another tenant's file.
        path = self._vec_index.get_path_for_mem_id_scoped(mem_id, user_id, scope_id)
        if path:
            abs_path = self._root_dir / path
            try:
                block = await self._md_store.read_block(abs_path, mem_id)
            except (OSError, UnicodeDecodeError) as e:
                # 读失败（权限/IO/编码）属临时故障：跳过此路径，继续走扫盘
                # fallback。若无防护，OSError 会冒泡到 list_memories 的 fallback
                # 分支把整条链打挂（FMI-DEF-003），与主路径降级语义不一致。
                memory_logger.warning(
                    "read_block failed on direct path lookup — degrading to scan fallback",
                    event_type=LogEventType.MEMORY_RETRIEVE,
                    user_id=user_id,
                    scope_id=scope_id,
                    operation="get_by_id.direct_path",
                    memory_id=[mem_id],
                    exception=str(e),
                    stacktrace=traceback.format_exc(),
                    metadata={"file_path": str(abs_path)},
                )
                block = None
            if block:
                return self._block_to_doc(block)

        # Fallback: scan all type files in scope dir. This is inherently
        # tenant-scoped — list_type_files only returns files under
        # memories/{user_id}/{scope_id}/, so it cannot leak cross-tenant.
        for type_file in self._md_store.list_type_files(user_id, scope_id):
            try:
                block = await self._md_store.read_block(type_file, mem_id)
            except (OSError, UnicodeDecodeError) as e:
                # 单个文件读失败不中断整个扫盘：跳过该文件继续下一个，
                # 尽力返回能读到的结果（降级语义与主路径一致）。
                memory_logger.warning(
                    "read_block failed during scan fallback — skipping file",
                    event_type=LogEventType.MEMORY_RETRIEVE,
                    user_id=user_id,
                    scope_id=scope_id,
                    operation="get_by_id.scan_fallback",
                    memory_id=[mem_id],
                    exception=str(e),
                    stacktrace=traceback.format_exc(),
                    metadata={"file_path": str(type_file)},
                )
                continue
            if block:
                return self._block_to_doc(block)

        return None

    async def list_memories(self, user_id: str, scope_id: str, offset: int = 0,
                            limit: int = 100, mem_types: list[str] | None = None,
                            *, filters: Optional[FilterGroup] = None) -> list[MemoryDoc]:
        """List memories with pagination and optional type filter.

        Uses chunks table for ID ordering (by updated_at DESC), then reads
        content from .md files for freshness.

        按 path（文件）分组后每个文件只调一次 ``read_blocks``，在内存中按
        mem_id 查 block，避免对同一文件的每条记忆重复读取解析（N+1 I/O）。
        ``list_chunks_meta`` 返回全量（无 SQL 分页），offset/limit 在
        Python 侧截断。

        Args:
            filters: Optional FilterGroup DSL predicate. The SQL layer
                pushes down ``EQ`` / ``NE`` on ``blacklisted`` /
                ``is_important`` (see ``VectorIndex._render_filter_group_to_sql``);
                any other field / operator is re-evaluated in Python via
                ``_apply_file_filter_group`` against the loaded MemoryDoc
                set, mirroring SimpleMemoryIndex's fallback strategy.
        """
        rows = self._vec_index.list_chunks_meta(
            user_id, scope_id, mem_types, filters=filters,
        )
        if not rows:
            return []

        # 按 path 分组，记录原始顺序（DB 返回按 updated_at DESC）
        path_to_ids: dict[str, list[str]] = {}
        order: list[tuple[str, str | None]] = []
        for mem_id, path in rows:
            if path:
                path_to_ids.setdefault(path, []).append(mem_id)
            order.append((mem_id, path))

        # 每个文件只 read_blocks 一次，构建 mem_id → block 映射
        block_by_id: dict[str, Block] = {}
        for path, mem_ids in path_to_ids.items():
            abs_path = self._root_dir / path
            if not abs_path.exists():
                continue
            try:
                blocks = await self._md_store.read_blocks(abs_path)
            except (OSError, UnicodeDecodeError):
                continue
            mem_id_set = set(mem_ids)
            for b in blocks:
                if b.mem_id in mem_id_set:
                    block_by_id[b.mem_id] = b

        # 按原始顺序产出 docs
        docs: list[MemoryDoc] = []
        for mem_id, path in order:
            block = block_by_id.get(mem_id)
            if block:
                docs.append(self._block_to_doc(block))
            else:
                # Fallback: 文件中未找到（罕见，如外部删除了该文件），
                # 扫描 scope 目录查找。get_by_id 内部已对 read 失败降级，
                # 此处再兜一层非预期异常，确保单条 mem_id 的兜底失败不会
                # 把整页分页查询打挂（降级返回部分结果而非崩）。
                try:
                    doc = await self.get_by_id(user_id, scope_id, mem_id)
                except (OSError, UnicodeDecodeError) as e:
                    memory_logger.warning(
                        "get_by_id fallback failed during pagination — skipping mem_id",
                        event_type=LogEventType.MEMORY_RETRIEVE,
                        user_id=user_id,
                        scope_id=scope_id,
                        operation="list_memories.fallback",
                        memory_id=[mem_id],
                        exception=str(e),
                        stacktrace=traceback.format_exc(),
                    )
                    continue
                if doc:
                    docs.append(doc)

        # Re-evaluate the full FilterGroup in Python. ``list_chunks_meta``
        # only pushes EQ/NE on blacklisted/is_important to SQL; conditions
        # on other fields (mem_type / arbitrary user fields) are dropped
        # there and must be evaluated against the loaded MemoryDoc set —
        # same fallback strategy SimpleMemoryIndex uses. The default
        # NE(blacklisted, True) path is a no-op here because those rows
        # never came back from SQL.
        if filters is not None and not _filter_group_is_pure_sql(filters):
            docs = [d for d in docs if _apply_filter_group_to_doc(d, filters)]

        return docs[offset:offset + limit]

    async def list_user_scopes(self) -> list[tuple[str, str]]:
        return self._md_store.list_user_scopes()

    async def search(self, user_id: str, scope_id: str, query: str,
                     mem_types: list[str] | None = None, top_k: int = 10,
                     *, filters: Optional[FilterGroup] = None) -> list[tuple[MemoryDoc, float]]:
        """Semantic search (vector + FTS5 hybrid).

        embedding 缺失时降级为 FTS-only：跳过 ``embed_query`` 与 ``_ensure_synced``，
        以空 query_vec 调下层 ``_vec_index.search``。其内部 ``_dims == len([])``
        不成立 → 走 ``_search_fallback``（``_cosine`` 长度不等返 0，vec 分量全 0
        不参与排序）+ FTS 分支正常执行，merged 以 BM25 关键词命中为主。

        降级路径跳过 ``_ensure_synced`` 是必要的：脏检查若发现文件变化会触发
        ``sync_file → upsert → _get_embedding``，无 embedding 时该路径抛
        RuntimeError。故用存量 FTS 索引尽力搜（embedding 故障是临时的，恢复
        后下次 search 会补同步）。API 临时故障时保住基本可用性，而非直接
        返回空；FTS5 基建（jieba 分词 + BM25）在无 embedding 时也可达。

        Args:
            filters: Optional FilterGroup DSL predicate. The SQL layer
                pushes down ``EQ`` / ``NE`` on ``blacklisted`` /
                ``is_important`` (see ``VectorIndex._render_filter_group_to_sql``);
                non-pushable conditions are re-evaluated in Python via
                ``_apply_filter_group_to_doc`` after the MemoryDoc lookup,
                matching SimpleMemoryIndex's fallback path. The default
                ``NE("blacklisted", True)`` injected by ``ensure_blacklisted_ne``
                is pushed down to SQL entirely, so no Python overhead
                lands on the common path.
        """
        if self._embedding_model:
            # Ensure all type files for this user/scope are synced (lazy, hash-based check).
            # 有 embedding 时才做完整同步：_ensure_synced 的脏检查若发现文件变化会触发
            # sync_file → upsert → _get_embedding，无 embedding 时该路径会抛
            # RuntimeError。降级 FTS-only 模式下跳过同步，用存量 FTS 索引尽力搜
            # （embedding 故障是临时的，恢复后下次 search 会补同步）。
            await self._ensure_synced(user_id, scope_id)
            query_vec = await self._embedding_model.embed_query(query)
        else:
            # 降级 FTS-only：记 warning（非 error）提示降级模式，便于排查
            # 向量召回变差，但搜索仍可用。
            memory_logger.warning(
                "Embedding model not initialized — degrading to FTS-only keyword search",
                event_type=LogEventType.MEMORY_RETRIEVE,
                scope_id=scope_id,
            )
            query_vec = []

        hits = await self._vec_index.search(
            TenantScope(user_id=user_id, scope_id=scope_id), query_vec,
            top_k=top_k,
            constraints=SearchConstraints(
                mem_types=mem_types,
                query_text=query,
                filters=filters,
            ),
        )

        # Resolve mem_ids to MemoryDocs
        results: list[tuple[MemoryDoc, float]] = []
        for mem_id, score in hits:
            path = self._vec_index.get_path_for_mem_id_scoped(mem_id, user_id, scope_id)
            if not path:
                continue
            abs_path = self._root_dir / path
            block = await self._md_store.read_block(abs_path, mem_id)
            if block is None:
                continue
            results.append((self._block_to_doc(block), score))

        if filters is not None and not _filter_group_is_pure_sql(filters):
            results = [
                (d, s) for (d, s) in results
                if _apply_filter_group_to_doc(d, filters)
            ]

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    # ------------------------------------------------------------------
    # schema version & backup
    # ------------------------------------------------------------------

    def get_schema_version(self) -> int:
        return self._schema_version

    def update_schema_version(self, version: int) -> None:
        self._schema_version = version

    async def create_backup(self) -> str:
        bid = str(uuid.uuid4())
        self._backups[bid] = {"schema_version": self._schema_version}
        return bid

    async def restore_backup(self, backup_id: str) -> None:
        if backup_id not in self._backups:
            raise ValueError(f"Backup {backup_id} not found")
        self._schema_version = self._backups[backup_id]["schema_version"]

    async def cleanup_backup(self, backup_id: str) -> None:
        self._backups.pop(backup_id, None)


# ------------------------------------------------------------------
# FilterGroup application helpers (module-level)
# ------------------------------------------------------------------
#
# FileMemoryIndex pushes EQ/NE on blacklisted/is_important to the chunks
# SQL layer (see VectorIndex._render_filter_group_to_sql). When a caller's
# FilterGroup also references other fields (mem_type / arbitrary user
# fields stored in the .md frontmatter), the SQL layer drops those
# conditions and we re-evaluate the full FilterGroup in Python against
# the loaded MemoryDoc set. Same fallback strategy as SimpleMemoryIndex.
_SQL_PUSHDOWN_FIELDS = frozenset({"blacklisted", "is_important"})


def _filter_group_is_pure_sql(group: FilterGroup) -> bool:
    """Return True iff every condition references a SQL-pushable field."""
    for cond in group.conditions:
        if isinstance(cond, FilterGroup):
            if not _filter_group_is_pure_sql(cond):
                return False
        else:
            if cond.field not in _SQL_PUSHDOWN_FIELDS:
                return False
    return True


def _apply_filter_group_to_doc(doc: MemoryDoc, group: FilterGroup) -> bool:
    """Application-layer FilterGroup evaluator for FileMemoryIndex.

    Mirrors SimpleMemoryIndex's ``_apply_filter_group`` so the two backends
    evaluate non-pushable conditions identically. Top-level MemoryDoc
    fields (``blacklisted`` / ``is_important``) take priority; otherwise
    the ``fields`` dict is consulted.
    """
    if not group.conditions:
        return True
    results = [_apply_filter_condition_or_group_to_doc(doc, c) for c in group.conditions]
    if group.logic == FilterLogic.AND:
        return all(results)
    return any(results)


def _apply_filter_condition_or_group_to_doc(doc: MemoryDoc, cond_or_group) -> bool:
    if isinstance(cond_or_group, FilterGroup):
        return _apply_filter_group_to_doc(doc, cond_or_group)
    return _apply_filter_condition_to_doc(doc, cond_or_group)


def _apply_filter_condition_to_doc(doc: MemoryDoc, cond) -> bool:
    if hasattr(doc, cond.field):
        value = getattr(doc, cond.field, None)
    else:
        value = doc.fields.get(cond.field)
    op = cond.op.value
    target = cond.value
    if op == "eq":
        return value == target
    if op == "ne":
        return value != target
    return False
