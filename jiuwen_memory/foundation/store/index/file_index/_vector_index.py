# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
SQLite-backed vector index for FileMemoryIndex.

Schema:

- ``files`` table — tracks per-``{Type}.md`` file hash/mtime/size for
  incremental change detection.
- ``chunks`` table — per-memory metadata + text + embedding BLOB, with
  ``path``/``start_line``/``end_line`` for block-level line-number tracking.
- ``chunks_vec`` (vec0, cosine + partition_key) — KNN 向量检索。
- ``chunks_fts`` (FTS5, jieba 分词) — 中文/英文关键词召回。
- ``embedding_cache`` — embedding 结果缓存，避免重复调 API。

Key feature: ``sync_file()`` performs **incremental reindex** — only
processing blocks that were added, modified, or deleted since last sync.
Unchanged-text blocks whose line numbers drifted get a cheap UPDATE instead
of a re-embed.

Search: sqlite-vec KNN with cosine distance + FTS5 hybrid (0.7/0.3 weighted),
pure-Python cosine fallback when sqlite-vec is unavailable.

.. warning::

    Single-process use only; no cross-process locking beyond WAL mode.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from jiuwen_memory.common.logging import store_logger
from jiuwen_memory.common.logging.events import LogEventType
from jiuwen_memory.foundation.codec import StorageCodec
from jiuwen_memory.foundation.store.base_memory_index import MemoryDoc
from jiuwen_memory.foundation.store.index.file_index._chunk_parser import (
    Block,
    hash_text,
    parse_blocks,
)
from jiuwen_memory.foundation.store.filter_dsl import FilterGroup, FilterLogic

if TYPE_CHECKING:
    pass

VECTOR_WEIGHT = 0.9
FTS_WEIGHT = 0.1
_OVERFETCH_MULTIPLIER = 2
_FTS_QUERY_MAX_TOKENS = 10


@dataclass(frozen=True)
class TenantScope:
    """租户坐标：user_id + scope_id，三者强相关且在全模块重复传递。

    封装为具名结构体以消除 ``G.FNM.03``（参数过多且相关）告警，
    同时让 search / sync_file / upsert 及其内部 ``_search_*`` 方法
    不再逐个透传这两个参数。
    """

    user_id: str
    scope_id: str


@dataclass(frozen=True)
class ChunkLocation:
    """单个 chunk 在 ``{Type}.md`` 文件中的物理定位。

    三元组 (path, start_line, end_line) 语义上是一个整体，
    封装后 :meth:`VectorIndex.upsert` 调用更清爽。
    """

    path: str = ""
    start_line: int = 0
    end_line: int = 0


@dataclass(frozen=True)
class SearchConstraints:
    """``VectorIndex.search`` 的内容侧约束——把可选的召回过滤器打包成
    一个具名结构体，避免 ``search`` 的形参列表超过 G.FNM.03 的 5 个上限。

    All fields optional with safe defaults so callers can construct a
    partial constraints object. ``mem_types`` / ``query_text`` /
    ``filters`` are all content-side filters applied post-KNN (vec0 KNN
    forbids auxiliary-column WHERE); ``top_k`` lives on the positional
    args of :meth:`VectorIndex.search` because it changes per call and
    every caller already threads it.
    """

    mem_types: Optional[list[str]] = None
    query_text: str = ""
    filters: Optional["FilterGroup"] = None


# 分隔符用于把 (user_id, scope_id) 组合成 vec0 的 partition_key。
# 用 \x1f (Unit Separator) 避免与 user_id/scope_id 自身的字符冲突。
_PARTITION_SEP = "\x1f"


def _partition_key(user_id: str, scope_id: str) -> str:
    """Combine (user_id, scope_id) into a single partition_key for vec0.

    sqlite-vec 0.1.9 的 vec0 表只支持单列 partition_key，且 KNN 查询时只接受
    partition_key 的等值约束（不允许对辅助列 +col 加 WHERE）。所以把强租户
    隔离维度 user_id+scope_id 拼成单列，KNN 直接在分区内执行。
    """
    return f"{user_id}{_PARTITION_SEP}{scope_id}"


def _segment_chinese(text: str) -> str:
    """用 jieba 分词后空格连接，供 FTS5 (unicode61) 按词索引。

    FTS5 默认 unicode61 tokenizer 把连续 CJK 当作单个 token，导致中文整串
    匹配、关键词检索基本失效。写入侧和查询侧都用本函数分词，unicode61 即可
    按空格切出真正的词。jieba 不可用时退化为单字 bigram（零依赖兜底）。

    ``chunks.text`` 存原文，FTS 存分词后文本 —— FTS 只用于 MATCH 返回 rowid，
    原文始终从 ``.md`` 文件读取，所以分词不影响对外暴露的内容。
    """
    if not text:
        return ""
    try:
        import jieba
        return " ".join(t for t in jieba.cut(text) if t.strip())
    except Exception:
        # 退化兜底：手动 bigram，保证 CJK 2 字窗口可检索
        out: list[str] = []
        for run in re.findall(r"[一-鿿]+", text):
            if len(run) == 1:
                out.append(run)
            else:
                out.extend(run[i:i + 2] for i in range(len(run) - 1))
        out.extend(re.findall(r"[a-zA-Z0-9]+", text))
        return " ".join(out)


class VectorIndex:
    """SQLite vector index with incremental file-based reindex."""

    def __init__(self, db_path: str, embedding_model: Any = None, codec: StorageCodec | None = None):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._embedding_model = embedding_model
        self._codec: StorageCodec | None = codec
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._vec_available = False
        self._dims: int | None = None
        self._fts_available = False
        self._ensure_schema()
        self._try_load_vec()
        self._recover_dims_if_exists()

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------

    @property
    def vec_available(self) -> bool:
        return self._vec_available

    @vec_available.setter
    def vec_available(self, value: bool) -> None:
        """Override vector availability (for testing fallback paths)."""
        self._vec_available = value

    @property
    def dims(self) -> int | None:
        return self._dims

    @dims.setter
    def dims(self, value: int | None) -> None:
        """Override the cached dimension (for testing recovery failure paths).

        测试模拟 ``_recover_dims_if_exists`` 恢复失败需将 ``_dims`` 强制置
        ``None``，通过此 setter 而非触达受保护成员。
        """
        self._dims = value

    @property
    def fts_available(self) -> bool:
        return self._fts_available

    @property
    def conn(self) -> sqlite3.Connection:
        """The underlying SQLite connection (read-only — for testing/inspection).

        测试与诊断脚本需直接执行 SQL 校验内部状态（如 chunks_vec 行数、
        手动 DROP 模拟表损坏），通过此 property 访问而非触达 ``_conn`` 受保护
        成员，遵循 G.CLS.11（不在类外访问受保护成员）。
        """
        return self._conn

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------

    def set_embedding_model(self, model: Any) -> None:
        self._embedding_model = model

    @property
    def embedding_model(self) -> Any:
        """The current embedding model (read-only)."""
        return self._embedding_model

    def set_codec(self, codec: StorageCodec) -> None:
        self._codec = codec

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist (idempotent)."""
        # --- files table ---
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                hash TEXT,
                mtime INTEGER,
                size INTEGER
            )
        """)
        # --- chunks table (with path/start_line/end_line for block tracking) ---
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                mem_id TEXT PRIMARY KEY,
                path TEXT,
                user_id TEXT,
                scope_id TEXT,
                type TEXT,
                start_line INTEGER DEFAULT 0,
                end_line INTEGER DEFAULT 0,
                hash TEXT,
                text TEXT,
                embedding BLOB,
                updated_at TEXT,
                blacklisted INTEGER DEFAULT 0,
                is_important INTEGER DEFAULT 0
            )
        """)
        # Idempotent migration for DBs created before the Ebbinghaus
        # forgetting columns existed. ``ALTER TABLE ADD COLUMN`` is a
        # no-op once the column is present (guarded by PRAGMA table_info);
        # without it, existing on-disk indexes would raise
        # ``table chunks has no column named blacklisted`` on first upsert.
        self._migrate_add_column_if_missing(
            "chunks", "blacklisted", "INTEGER DEFAULT 0"
        )
        self._migrate_add_column_if_missing(
            "chunks", "is_important", "INTEGER DEFAULT 0"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_scope ON chunks(user_id, scope_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_user_scope_type ON chunks(user_id, scope_id, type)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)"
        )
        # --- embedding cache [unchanged] ---
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_cache (
                provider TEXT,
                model TEXT,
                hash TEXT PRIMARY KEY,
                embedding BLOB,
                dims INTEGER,
                updated_at TEXT
            )
        """)
        # --- FTS5 [unchanged] ---
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text)"
            )
            self._fts_available = True
        except Exception as e:
            self._fts_available = False
            store_logger.warning(
                "Failed to create FTS5 table, keyword search disabled",
                event_type=LogEventType.STORE_RETRIEVE,
                exception=str(e),
            )
        self._conn.commit()

    def _migrate_add_column_if_missing(
        self, table: str, column: str, decl: str
    ) -> None:
        """Add ``column`` to ``table`` if absent; no-op if already present.

        SQLite has no ``ADD COLUMN IF NOT EXISTS``; PRAGMA table_info is
        the portable way to probe. Used for the Ebbinghaus forgetting
        scalar columns on pre-existing ``chunks`` tables created before
        those columns existed. Runs inside ``_ensure_schema`` so a fresh
        init and a legacy-DB reopen both end up with the same schema.
        """
        existing = {
            r[1]
            for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            try:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )
            except sqlite3.Error as e:
                store_logger.warning(
                    "Failed to add column %s to %s during migration: %s",
                    column, table, e,
                    event_type=LogEventType.STORE_RETRIEVE,
                )

    def _try_load_vec(self) -> None:
        try:
            import sqlite_vec
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            self._vec_available = True
        except Exception as e:
            self._vec_available = False
            store_logger.warning(
                "sqlite-vec not available, vector search will use pure-Python fallback",
                event_type=LogEventType.STORE_RETRIEVE,
                exception=str(e),
            )

    def _recover_dims_if_exists(self) -> None:
        """从磁盘 ``chunks_vec`` 表恢复 ``self._dims``，避免重启后误 DROP。

        ``_dims`` 是内存变量，``__init__`` 置 ``None`` 且启动时本不读回。本方法
        在启动（sqlite-vec 扩展加载后）探测 ``chunks_vec`` 是否已存在；若存在，
        从 ``sqlite_master`` 的建表 SQL（形如
        ``... USING vec0(embedding float[1024] ...)``）正则提取维度赋给
        ``_dims``。``PRAGMA table_info`` 对 vec0 虚拟表返回空类型，不可用，
        只能从原始 SQL 提取。表不存在（真正首次启动）或探测失败时静默，
        ``_dims`` 保持 ``None``——``_ensure_vec_table`` 下方的
        ``CREATE VIRTUAL TABLE IF NOT EXISTS`` 保证"表不存在则建、表存在则跳过"，
        不会因恢复失败而误 DROP 存量向量（见 _ensure_vec_table 的注释）。
        """
        if not self._vec_available:
            return
        try:
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
            ).fetchone()
            if not row or not row[0]:
                return  # 表不存在 —— 首次启动，_dims 保持 None
            m = re.search(r"embedding\s+float\[(\d+)\]", row[0])
            if not m:
                return  # SQL 损坏/旧格式 —— 降级为首次建表
            self._dims = int(m.group(1))
            store_logger.info(
                "Recovered chunks_vec dims=%d from disk (no DROP on first upsert)",
                self._dims,
                event_type=LogEventType.STORE_RETRIEVE,
            )
        except sqlite3.Error as e:
            # 探测失败不致命：_dims 保持 None，_ensure_vec_table 仍会建表
            # （可能 DROP 已损坏的表，可接受）
            store_logger.warning(
                "Failed to recover chunks_vec dims from disk",
                event_type=LogEventType.STORE_RETRIEVE,
                exception=str(e),
            )

    def _ensure_vec_table(self, dims: int) -> bool:
        if not self._vec_available:
            return False
        if self._dims is not None and self._dims != dims:
            try:
                self._conn.execute("DROP TABLE IF EXISTS chunks_vec")
            except sqlite3.Error as e:
                store_logger.warning(
                    "Failed to drop chunks_vec during dimension migration",
                    event_type=LogEventType.STORE_RETRIEVE,
                    exception=str(e),
                )
            self._dims = None
        if self._dims == dims:
            return True
        # "不知道维度" ≠ "该清表"。_recover_dims_if_exists 恢复失败时 _dims
        # 保持 None 但 chunks_vec 有存量数据，DROP 是毁灭性的。不再靠猜测式
        # DROP —— 下面的 CREATE VIRTUAL TABLE IF NOT EXISTS 由 SQL 自身保证
        # "表不存在则建，表存在则跳过"。真正需要换表（维度迁移）已在上方
        # _dims != dims 分支的 DROP 中处理。
        try:
            # distance_metric=cosine: bge-m3 等模型输出归一化向量，cosine 距离
            #   返回 1 - 余弦相似度 ∈ [0, 2]，对语义相似度有良好区分度
            #   （vec0 默认是 L2，归一化向量间 L2 都很小 → 分数无区分度）。
            # partition_key: 把 user_id/scope_id 组合成单列（"uid\x1fsid"），
            #   KNN 在分区内执行，避免 overfetch-then-filter 在多租户场景下漏
            #   召回。sqlite-vec 0.1.9 不允许在 KNN 查询里对辅助列（+col）加
            #   WHERE 约束，但 partition_key 支持等值过滤，所以用 partition_key
            #   承载强租户隔离。mem_types 是可选多值过滤，仍后置在 chunks 上。
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
                f"embedding float[{dims}] distance_metric=cosine, "
                f"partition_key text)"
            )
            self._dims = dims
            return True
        except Exception:
            self._vec_available = False
            return False

    # ------------------------------------------------------------------
    # vector ↔ blob helpers [unchanged]
    # ------------------------------------------------------------------

    @staticmethod
    def _vector_to_blob(vec: list[float]) -> bytes:
        return struct.pack(f"{len(vec)}f", *vec)

    @staticmethod
    def _blob_to_vector(blob: bytes) -> list[float]:
        n = len(blob) // 4
        return list(struct.unpack(f"{n}f", blob))

    # ------------------------------------------------------------------
    # file hash
    # ------------------------------------------------------------------

    @staticmethod
    def file_hash(content: str) -> str:
        """SHA256 first 16 chars of file content (used for files table)."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------
    # embedding
    # ------------------------------------------------------------------

    def _provider_key(self) -> tuple[str, str]:
        model = self._embedding_model
        name = type(model).__name__ if model is not None else "none"
        return name, name

    async def _get_embedding(self, text: str) -> list[float]:
        if self._embedding_model is None:
            raise RuntimeError("embedding model not initialized")
        h = hash_text(text)
        provider, model = self._provider_key()
        row = self._conn.execute(
            "SELECT embedding, dims FROM embedding_cache WHERE provider=? AND model=? AND hash=?",
            (provider, model, h),
        ).fetchone()
        if row and row[0]:
            return self._blob_to_vector(row[0])
        vec = await self._embedding_model.embed_documents([text])
        if not vec:
            return []
        v = vec[0]
        self._conn.execute(
            "INSERT OR REPLACE INTO embedding_cache(provider, model, hash, embedding, dims, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (provider, model, h, self._vector_to_blob(v), len(v),
             datetime.now(timezone.utc).astimezone().isoformat()),
        )
        self._conn.commit()
        return v

    # ------------------------------------------------------------------
    # upsert / delete (single chunk)
    # ------------------------------------------------------------------

    async def upsert(
        self,
        doc: MemoryDoc,
        tenant: TenantScope,
        location: ChunkLocation | None = None,
    ) -> None:
        """Insert or update a single chunk in the vector index.

        Args:
            doc: The memory document.
            tenant: Owner (user_id, scope_id).
            location: Chunk's physical location in its ``{Type}.md`` file.
                ``None`` defaults to an empty location (path/lines unset).
        """
        user_id, scope_id = tenant.user_id, tenant.scope_id
        path = location.path if location else ""
        start_line = location.start_line if location else 0
        end_line = location.end_line if location else 0
        text = doc.text
        vec = await self._get_embedding(text)
        if vec:
            self._ensure_vec_table(len(vec))
            if self._dims is None:
                self._dims = len(vec)
        h = hash_text(text)
        blob = self._vector_to_blob(vec) if vec else None
        ts = (doc.timestamp or datetime.now(timezone.utc).astimezone()).isoformat()
        # Ebbinghaus forgetting flags: persisted to the chunks table so
        # list_chunks_meta can render a FilterGroup to a SQL WHERE clause
        # without re-reading the .md file, and search can post-filter
        # KNN candidates the same way (vec0 KNN forbids auxiliary-column
        # WHERE, so search filters after rowid retrieval — see _search_vec).
        bl = 1 if getattr(doc, "blacklisted", False) else 0
        imp = 1 if getattr(doc, "is_important", False) else 0
        self._conn.execute(
            "INSERT INTO chunks(mem_id, path, user_id, scope_id, type, start_line, end_line, "
            "hash, text, embedding, updated_at, blacklisted, is_important) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(mem_id) DO UPDATE SET "
            "path=excluded.path, user_id=excluded.user_id, scope_id=excluded.scope_id, "
            "type=excluded.type, start_line=excluded.start_line, end_line=excluded.end_line, "
            "hash=excluded.hash, text=excluded.text, embedding=excluded.embedding, "
            "updated_at=excluded.updated_at, "
            "blacklisted=excluded.blacklisted, is_important=excluded.is_important",
            (doc.id, path, user_id, scope_id, doc.type, start_line, end_line,
             h, text, blob, ts, bl, imp),
        )
        rowid = self._conn.execute(
            "SELECT rowid FROM chunks WHERE mem_id=?", (doc.id,)
        ).fetchone()
        if rowid:
            rid = rowid[0]
            if vec and self._vec_available and self._dims:
                try:
                    self._conn.execute("DELETE FROM chunks_vec WHERE rowid=?", (rid,))
                except sqlite3.Error as e:
                    store_logger.warning(
                        "Failed to delete row from chunks_vec before upsert",
                        event_type=LogEventType.STORE_RETRIEVE,
                        exception=str(e),
                    )
                # 带 partition_key 列写入，KNN 在 (user_id, scope_id) 分区内执行
                self._conn.execute(
                    "INSERT INTO chunks_vec(rowid, embedding, partition_key) "
                    "VALUES(?,?,?)",
                    (rid, blob, _partition_key(user_id, scope_id)),
                )
            if self._fts_available:
                try:
                    self._conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (rid,))
                except sqlite3.Error as e:
                    store_logger.warning(
                        "Failed to delete row from chunks_fts before upsert",
                        event_type=LogEventType.STORE_RETRIEVE,
                        exception=str(e),
                    )
                # jieba 分词后写入 FTS：unicode61 按空格切，中文才能真正按词索引
                self._conn.execute(
                    "INSERT INTO chunks_fts(rowid, text) VALUES(?,?)",
                    (rid, _segment_chinese(text)),
                )
        self._conn.commit()

    def get_text(self, mem_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT text FROM chunks WHERE mem_id=?", (mem_id,)
        ).fetchone()
        return row[0] if row else None

    def get_path_for_mem_id_scoped(
        self, mem_id: str, user_id: str, scope_id: str
    ) -> str | None:
        """Return the path for *mem_id* **only if** it belongs to *user_id*/*scope_id*.
        """
        row = self._conn.execute(
            "SELECT path FROM chunks WHERE mem_id=? AND user_id=? AND scope_id=?",
            (mem_id, user_id, scope_id),
        ).fetchone()
        return row[0] if row and row[0] else None

    def get_chunk_line_numbers(self, mem_id: str) -> tuple[int, int] | None:
        """Return ``(start_line, end_line)`` for *mem_id*, or ``None``."""
        row = self._conn.execute(
            "SELECT start_line, end_line FROM chunks WHERE mem_id=?", (mem_id,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def delete_chunk_by_mem_id(self, mem_id: str) -> None:
        """Delete a single chunk row by *mem_id* and commit immediately.

        .. warning::
            Bypasses file-sync logic. Intended for testing scenarios
            that need to simulate orphaned / unindexed records.
        """
        self._conn.execute("DELETE FROM chunks WHERE mem_id=?", (mem_id,))
        self._conn.commit()

    def list_chunks_meta(
        self,
        user_id: str,
        scope_id: str,
        mem_types: list[str] | None = None,
        filters: Optional["FilterGroup"] = None,
    ) -> list[tuple[str, str]]:
        """Return ``(mem_id, path)`` tuples for chunks matching the filter, newest first.

        Encapsulates the chunks-table query so callers (e.g. the orchestration
        layer's ``list_memories``) don't reach into ``_conn`` directly. Used to
        order memories by ``updated_at DESC`` for pagination; content is then
        read from the ``.md`` files for freshness.

        Args:
            filters: Optional FilterGroup DSL predicate. Only ``EQ`` / ``NE``
                on ``blacklisted`` / ``is_important`` translate to SQL (same
                subset SimpleMemoryIndex pushes down to its vector scalar
                schema). Any other field / operator is ignored at the SQL
                layer and the caller is responsible for re-evaluating the
                full FilterGroup in Python against the returned MemoryDoc
                set (see FileMemoryIndex.list_memories).
        """
        where_sql = "user_id=? AND scope_id=?"
        params: list = [user_id, scope_id]
        if mem_types:
            placeholders = ",".join("?" for _ in mem_types)
            where_sql += f" AND type IN ({placeholders})"
            params.extend(mem_types)
        sql_filter, sql_params = self._render_filter_group_to_sql(filters)
        if sql_filter:
            where_sql += f" AND ({sql_filter})"
            params.extend(sql_params)
        try:
            rows = self._conn.execute(
                f"SELECT mem_id, path FROM chunks WHERE {where_sql} ORDER BY updated_at DESC",
                params,
            ).fetchall()
        except Exception:
            return []
        return [(r[0], r[1]) for r in rows if r[0]]

    def update_chunk_scalars(
        self,
        mem_id: str,
        scalars: dict[str, Any],
    ) -> None:
        """In-place UPDATE of scalar columns on the chunks row for *mem_id*.

        Used by :meth:`FileMemoryIndex.update_mem_by_id` to persist
        ``blacklisted`` / ``is_important`` flips without a re-embedding
        roundtrip. ``sync_file`` only re-upserts text-changed blocks
        (it classifies by body hash), so a pure-scalar flip would
        otherwise be lost from the chunks table between syncs — this
        direct UPDATE keeps the SQL index in sync immediately.

        Only ``blacklisted`` / ``is_important`` are accepted; other
        keys are ignored (arbitrary user fields live in the .md
        frontmatter ``fields:`` map, not the chunks schema).
        """
        if not scalars or not mem_id:
            return
        assignments: list[str] = []
        params: list = []
        for name, value in scalars.items():
            if name == "blacklisted":
                assignments.append("blacklisted=?")
                params.append(1 if bool(value) else 0)
            elif name == "is_important":
                assignments.append("is_important=?")
                params.append(1 if bool(value) else 0)
        if not assignments:
            return
        # Refresh updated_at so list_chunks_meta's ORDER BY updated_at DESC
        # surfaces recently-touched memories at the top of pagination.
        assignments.append("updated_at=?")
        params.append(datetime.now(timezone.utc).astimezone().isoformat())
        params.append(mem_id)
        try:
            self._conn.execute(
                f"UPDATE chunks SET {', '.join(assignments)} WHERE mem_id=?",
                params,
            )
            self._conn.commit()
        except sqlite3.Error as e:
            store_logger.warning(
                "Failed to update chunk scalars for %s: %s", mem_id, e,
                event_type=LogEventType.STORE_RETRIEVE,
            )

    # ------------------------------------------------------------------
    # FilterGroup → SQL rendering (EQ/NE on blacklisted / is_important only)
    # ------------------------------------------------------------------

    #: Fields eligible for SQL pushdown. Matches SimpleMemoryIndex's
    #: ``_VECTOR_PUSHDOWN_FIELDS`` so the two backends honour the same
    #: subset of the FilterGroup DSL on the same fields.
    _SQL_PUSHDOWN_FIELDS = frozenset({"blacklisted", "is_important"})

    def _render_filter_group_to_sql(
        self, group: Optional["FilterGroup"],
    ) -> tuple[str, list]:
        """Render a FilterGroup to a SQL WHERE fragment + bound params.

        Returns ``("", [])`` when ``group`` is ``None`` or empty. Only
        ``EQ`` / ``NE`` conditions on ``blacklisted`` / ``is_important``
        are translated — the same subset SimpleMemoryIndex pushes down
        to its vector scalar schema. Any condition on another field is
        silently dropped here (callers must re-evaluate the full
        FilterGroup in Python if they need completeness); this mirrors
        SimpleMemoryIndex's "mixed group falls back to app layer" stance
        and keeps the SQL layer a pure optimisation, not a correctness
        surface.

        Booleans are stored in SQLite as 0/1 (INTEGER), so EQ/NE values
        are coerced via ``int(bool(...))``. Non-boolean scalars (str /
        int / float / None) are passed through unchanged.
        """
        if group is None:
            return "", []
        if not isinstance(group, FilterGroup) or not group.conditions:
            return "", []
        parts: list[str] = []
        params: list = []
        for cond in group.conditions:
            if isinstance(cond, FilterGroup):
                sub_sql, sub_params = self._render_filter_group_to_sql(cond)
                if sub_sql:
                    parts.append(f"({sub_sql})")
                    params.extend(sub_params)
                continue
            if cond.field not in self._SQL_PUSHDOWN_FIELDS:
                # Non-pushable field — caller re-evaluates in Python.
                continue
            value = cond.value
            if isinstance(value, bool):
                value = 1 if value else 0
            op_sql = "=" if cond.op.value == "eq" else "!="
            parts.append(f"{cond.field} {op_sql} ?")
            params.append(value)
        if not parts:
            return "", []
        joiner = " AND " if group.logic == FilterLogic.AND else " OR "
        return joiner.join(parts), params

    def delete(self, mem_id: str) -> None:
        rowid = self._conn.execute(
            "SELECT rowid FROM chunks WHERE mem_id=?", (mem_id,)
        ).fetchone()
        self._conn.execute("DELETE FROM chunks WHERE mem_id=?", (mem_id,))
        if rowid and self._vec_available:
            try:
                self._conn.execute("DELETE FROM chunks_vec WHERE rowid=?", (rowid[0],))
            except sqlite3.Error as e:
                store_logger.warning(
                    "Failed to delete row from chunks_vec",
                    event_type=LogEventType.STORE_RETRIEVE,
                    exception=str(e),
                )
        if rowid and self._fts_available:
            try:
                self._conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (rowid[0],))
            except sqlite3.Error as e:
                store_logger.warning(
                    "Failed to delete row from chunks_fts",
                    event_type=LogEventType.STORE_RETRIEVE,
                    exception=str(e),
                )
        self._conn.commit()

    def delete_by_path(self, path: str) -> None:
        """Delete all chunks belonging to a ``{Type}.md`` file, plus its
        ``files`` table entry.

        Used when a type file is deleted outright (e.g. the last memory in it
        is removed, or the user deletes the file). Encapsulates chunks_vec /
        chunks_fts cleanup so callers don't reach into the connection.
        """

        sql = "SELECT rowid FROM chunks WHERE path=?"
        cur = self._conn.execute(sql, (path,))
        rowids = [r[0] for r in cur.fetchall()]
        self._conn.execute("DELETE FROM chunks WHERE path=?", (path,))
        self._delete_vec_fts_rows(rowids)
        self._conn.execute("DELETE FROM files WHERE path=?", (path,))
        self._conn.commit()

    # ------------------------------------------------------------------
    # batch delete
    # ------------------------------------------------------------------

    def delete_by_user(self, user_id: str) -> None:
        sql = "SELECT rowid FROM chunks WHERE user_id=?"
        cur = self._conn.execute(sql, (user_id,))
        rowids = [r[0] for r in cur.fetchall()]
        self._conn.execute("DELETE FROM chunks WHERE user_id=?", (user_id,))
        self._delete_vec_fts_rows(rowids)
        # Also clean up files table entries under this user
        self._conn.execute(
            "DELETE FROM files WHERE path LIKE ?",
            (f"memories/{user_id}/%",),
        )
        self._conn.commit()

    def delete_by_scope(self, scope_id: str) -> None:
        sql = "SELECT rowid FROM chunks WHERE scope_id=?"
        cur = self._conn.execute(sql, (scope_id,))
        rowids = [r[0] for r in cur.fetchall()]
        self._conn.execute("DELETE FROM chunks WHERE scope_id=?", (scope_id,))
        self._delete_vec_fts_rows(rowids)
        self._conn.execute(
            "DELETE FROM files WHERE path LIKE ?",
            (f"memories/%/{scope_id}/%",),
        )
        self._conn.commit()

    def delete_by_user_and_scope(self, user_id: str, scope_id: str) -> None:
        sql = "SELECT rowid FROM chunks WHERE user_id=? AND scope_id=?"
        cur = self._conn.execute(sql, (user_id, scope_id))
        rowids = [r[0] for r in cur.fetchall()]
        self._conn.execute(
            "DELETE FROM chunks WHERE user_id=? AND scope_id=?", (user_id, scope_id)
        )
        self._delete_vec_fts_rows(rowids)
        self._conn.execute(
            "DELETE FROM files WHERE path LIKE ?",
            (f"memories/{user_id}/{scope_id}/%",),
        )
        self._conn.commit()

    def _delete_vec_fts_rows(self, rowids: list[int]) -> None:
        if rowids and self._vec_available:
            # 与下方 chunks_fts 对称的异常保护：chunks 主表已在调用方删除
            # （语义已正确），vec/fts 残留仅产生无害孤立行（search 按 rowid
            # 回查 chunks 表查不到 → 不召回已删记忆）。吞掉异常让后续 files
            # 表清理与 commit 能完成，避免半成功状态恶化；表损坏/DB schema
            # 不一致/锁竞争时记 warning 便于排查。
            try:
                self._conn.executemany(
                    "DELETE FROM chunks_vec WHERE rowid=?", [(r,) for r in rowids]
                )
            except sqlite3.Error as e:
                store_logger.warning(
                    "Failed to batch-delete rows from chunks_vec",
                    event_type=LogEventType.STORE_RETRIEVE,
                    exception=str(e),
                )
        if rowids and self._fts_available:
            try:
                self._conn.executemany(
                    "DELETE FROM chunks_fts WHERE rowid=?", [(r,) for r in rowids]
                )
            except sqlite3.Error as e:
                store_logger.warning(
                    "Failed to batch-delete rows from chunks_fts",
                    event_type=LogEventType.STORE_RETRIEVE,
                    exception=str(e),
                )

    # ------------------------------------------------------------------
    # incremental sync — core feature
    # ------------------------------------------------------------------

    async def sync_file(
        self,
        path: str,              # e.g. "memories/u1/s1/user_profile.md"
        tenant: TenantScope,
        type_name: str,
        file_content: str,      # raw file content (for files table hash)
        blocks: list[Block] | None = None,  # decoded blocks (for embedding)
    ) -> None:
        """Incremental reindex for a single ``{Type}.md`` file.

        1. Use *blocks* if provided (already decoded), otherwise parse from *file_content*.
        2. Query DB for existing chunks on this path.
        3. Classify each block: upsert (new/changed) or delete (removed).
        4. Execute changes + commit.
        5. Update ``files`` table entry.

        .. important::

            When a :class:`StorageCodec` is active, *blocks* must contain
            **decoded** body text so that embeddings are computed on
            plaintext. The *file_content* is always the raw on-disk
            bytes and is used only for the ``files`` table hash.

        Args:
            path: Relative path to the ``{Type}.md`` file.
            tenant: Owner (user_id, scope_id).
            type_name: Memory type (should match file name stem).
            file_content: The raw text content of the file (may be encrypted).
            blocks: Pre-parsed, decoded blocks. If ``None``, parse from *file_content*.
        """
        # 1. Get blocks (use provided decoded blocks, or parse from raw content).
        #    file_blocks always carry accurate start_line/end_line for the
        #    current on-disk content (parse_blocks computes them, and
        #    blocks_to_markdown recalculates them on write).
        file_blocks: list[Block]
        if blocks is not None:
            file_blocks = blocks
        else:
            file_blocks = parse_blocks(file_content)
        file_map: dict[str, Block] = {b.mem_id: b for b in file_blocks}

        # 2. Query current DB state for this path
        db_rows = self._conn.execute(
            "SELECT mem_id, hash, start_line, end_line FROM chunks WHERE path=?",
            (path,),
        ).fetchall()
        db_map: dict[str, tuple[str, int, int]] = {
            r[0]: (r[1], r[2], r[3]) for r in db_rows
        }
        db_ids = set(db_map.keys())
        file_ids = set(file_map.keys())

        # 3. Classify
        to_upsert: list[Block] = []        # new or text-changed blocks → re-embed
        to_reline: list[Block] = []        # unchanged text but line numbers shifted
        to_delete: list[str] = []

        for bid in file_ids:
            block = file_map[bid]
            if bid not in db_ids:
                # New block
                to_upsert.append(block)
            elif block.hash != db_map[bid][0]:
                # Modified block (text hash changed)
                to_upsert.append(block)
            elif (block.start_line, block.end_line) != (db_map[bid][1], db_map[bid][2]):
                # Text unchanged but line numbers drifted (a sibling block above
                # grew/shrunk). Cheap fix: UPDATE line numbers without re-embedding.
                to_reline.append(block)
            # else: fully unchanged → skip

        for bid in db_ids:
            if bid not in file_ids:
                # Removed from file
                to_delete.append(bid)

        # Short-circuit if nothing changed
        if not to_upsert and not to_reline and not to_delete:
            # Still update files table (mtime/size may have changed, hash unchanged)
            self._upsert_file_record(path, file_content)
            self._conn.commit()
            return

        # 4. Execute deletions
        for mem_id in to_delete:
            self.delete(mem_id)

        # 5. Execute upserts (only changed/new blocks get fresh embeddings)
        for block in to_upsert:
            doc = MemoryDoc(
                id=block.mem_id,
                text=block.text,
                type=block.type,
                fields=block.fields,
                blacklisted=block.blacklisted,
                is_important=block.is_important,
            )
            await self.upsert(
                doc,
                tenant=tenant,
                location=ChunkLocation(
                    path=path,
                    start_line=block.start_line,
                    end_line=block.end_line,
                ),
            )

        # 6. Fix line numbers for unchanged-text blocks whose position drifted.
        #    This is the line-number rebase:
        #    instead of a delta-offset over trailing blocks, we directly write
        #    the accurate current line numbers, since file_blocks always carry
        #    them. No embedding recompute needed.
        for block in to_reline:
            self._conn.execute(
                "UPDATE chunks SET start_line=?, end_line=? WHERE mem_id=?",
                (block.start_line, block.end_line, block.mem_id),
            )

        # 7. Update files table
        self._upsert_file_record(path, file_content)
        self._conn.commit()

    def _upsert_file_record(self, path: str, content: str) -> None:
        """Update the ``files`` table entry for *path* with current hash/size."""
        fhash = self.file_hash(content)
        fsize = len(content.encode("utf-8"))
        self._conn.execute(
            "INSERT OR REPLACE INTO files(path, hash, mtime, size) VALUES(?,?,?,?)",
            (path, fhash, int(datetime.now(timezone.utc).timestamp() * 1000), fsize),
        )

    def get_file_hash_from_db(self, path: str) -> str | None:
        """Return the stored hash for *path* from the ``files`` table.

        Dirty-checking (comparing this against the on-disk file hash) is
        intentionally done by the orchestration layer (:class:`FileMemoryIndex`),
        which owns ``root_dir`` and can read the file. The vector index only
        exposes the stored side here.
        """
        row = self._conn.execute(
            "SELECT hash FROM files WHERE path=?", (path,)
        ).fetchone()
        return row[0] if row else None

    def delete_file_record(self, path: str) -> None:
        """Remove a file entry from the ``files`` table (used when file is deleted)."""
        self._conn.execute("DELETE FROM files WHERE path=?", (path,))
        self._conn.commit()

    # ------------------------------------------------------------------
    # search (unchanged from V1)
    # ------------------------------------------------------------------

    @staticmethod
    def _cosine(vec1: list[float], vec2: list[float]) -> float:
        if len(vec1) != len(vec2):
            return 0.0
        dot = sum(a * b for a, b in zip(vec1, vec2))
        n1 = math.sqrt(sum(a * a for a in vec1))
        n2 = math.sqrt(sum(b * b for b in vec2))
        if n1 < 1e-10 or n2 < 1e-10:
            return 0.0
        return dot / (n1 * n2)

    @staticmethod
    def build_fts_query(query: str) -> str:
        cleaned = (query or "").strip()
        if not cleaned:
            return ""
        # jieba 分词后查询，与写入侧 _segment_chinese 对齐。词长≥2 加前缀通配
        # （"茉莉"* 命中"茉莉花茶"），1 字词精确匹配避免短词噪声。
        try:
            import jieba
            tokens = [t for t in jieba.cut(cleaned) if t.strip()]
        except Exception:
            # 与 _segment_chinese 的兜底一致：CJK bigram + 英文按词
            tokens = []
            for run in re.findall(r"[一-鿿]+", cleaned):
                if len(run) == 1:
                    tokens.append(run)
                else:
                    tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
            tokens.extend(re.findall(r"[a-zA-Z0-9]+", cleaned))
        if not tokens:
            return ""
        terms = []
        for t in tokens[:_FTS_QUERY_MAX_TOKENS]:
            if len(t) >= 2:
                terms.append(f'"{t}"*')
            else:
                terms.append(f'"{t}"')
        return " OR ".join(terms)

    @staticmethod
    def bm25_rank_to_score(rank: float) -> float:
        """Convert SQLite FTS5 bm25 rank to a [0,1] relevance score.

        FTS5 的 rank 是**负数** bm25 分数，越负越相关（``ORDER BY rank`` 升序
        取最相关）。转成融合分时必须随相关度**单调递增**，否则关键词命中越强
        的记忆融合分越低、反而排到后面。

        负数分支用 ``m/(1+m)``（m = -rank = bm25 绝对分）：m 越大（越相关）→
        分数越接近 1；m→0（弱相关）→ 分数→0。值域 (0,1)，与向量 cosine
        分数量纲一致，融合权重 VECTOR_WEIGHT/FTS_WEIGHT 乘上去量纲对齐。
        """
        if rank >= 0:
            # FTS5 bm25 正常命中恒返回负 rank；rank>=0 属异常/无信息，
            # 给 0 分让它不参与融合（抢分），比原 1/(1+rank) 的满分更安全。
            return 0.0
        m = -rank
        return m / (1.0 + m)

    def _search_vec(self, tenant: TenantScope, query_vec: list[float],
                    mem_types: list[str] | None, top_k: int,
                    filters: Optional["FilterGroup"] = None) -> list[tuple[str, float]]:
        # KNN 在 (user_id, scope_id) 分区内执行：partition_key 等值约束让
        # sqlite-vec 只在目标租户的向量集合上做近邻搜索，避免 overfetch-
        # then-filter 在多租户场景下漏召回。embedding MATCH ? + k=? 触发 vec0
        # 的 ANN 路径，distance 列此时才有值（无 MATCH 时 distance 恒为 0 →
        # 分数全 1.0 的 bug）。mem_types / FilterGroup 后置在 chunks 上。
        #
        # vec0 KNN 不允许对辅助列（如 blacklisted）加 WHERE，所以 scalar
        # 过滤只能落在 chunks 表的 rowid join 上。filter 召回不足时升级 k
        # 重试一次：NE(blacklisted, True) 在重度遗忘的 scope 里可能丢弃大
        # 半候选，单次 overfetch 给不齐 top_k。
        user_id, scope_id = tenant.user_id, tenant.scope_id
        query_blob = self._vector_to_blob(query_vec)
        pkey = _partition_key(user_id, scope_id)

        sql_filter, sql_params = self._render_filter_group_to_sql(filters)

        for attempt_k in (top_k, top_k * 4):
            knn_rows = self._conn.execute(
                "SELECT rowid, distance FROM chunks_vec "
                "WHERE embedding MATCH ? AND k=? AND partition_key=? "
                "ORDER BY distance",
                (query_blob, attempt_k, pkey),
            ).fetchall()
            if not knn_rows:
                return []
            rowid_to_dist = {r[0]: float(r[1] or 0.0) for r in knn_rows}

            # mem_types / scalar filters 后置：候选集已收敛到目标 user/scope
            # 的近邻，成本可忽略。
            where_sql = "user_id=? AND scope_id=?"
            params: list = [user_id, scope_id]
            if mem_types:
                placeholders = ",".join("?" for _ in mem_types)
                where_sql += f" AND type IN ({placeholders})"
                params.extend(mem_types)
            if sql_filter:
                where_sql += f" AND ({sql_filter})"
                params.extend(sql_params)
            where_sql += f" AND rowid IN ({','.join('?' for _ in rowid_to_dist)})"
            params.extend(rowid_to_dist.keys())
            rows = self._conn.execute(
                f"SELECT mem_id, rowid FROM chunks WHERE {where_sql}",
                params,
            ).fetchall()

            results = []
            for mem_id, rowid in rows:
                dist = rowid_to_dist.get(rowid, 2.0)
                # cosine distance ∈ [0, 2] → score ∈ [0, 1]
                score = max(0.0, 1.0 - dist / 2.0)
                results.append((mem_id, score))
            results.sort(key=lambda x: x[1], reverse=True)
            if len(results) >= top_k or attempt_k == top_k * 4:
                return results
        return results

    def _search_fallback(self, tenant: TenantScope, query_vec: list[float],
                         mem_types: list[str] | None, top_k: int,
                         filters: Optional["FilterGroup"] = None) -> list[tuple[str, float]]:
        # 空 query_vec（embedding 缺失降级路径）短路返回空：否则会对所有存量向量
        # 算 cosine（_cosine 长度不等返 0），返回全 0 分结果污染 merged —— 让
        # 无向量命中也以 0 分挤进结果集。降级时向量召回本就无意义，直接返回空
        # 让 FTS 分支独立提供结果。
        if not query_vec:
            return []
        user_id, scope_id = tenant.user_id, tenant.scope_id
        where_sql = "user_id=? AND scope_id=? AND embedding IS NOT NULL"
        params: list = [user_id, scope_id]
        if mem_types:
            placeholders = ",".join("?" for _ in mem_types)
            where_sql += f" AND type IN ({placeholders})"
            params.extend(mem_types)
        sql_filter, sql_params = self._render_filter_group_to_sql(filters)
        if sql_filter:
            where_sql += f" AND ({sql_filter})"
            params.extend(sql_params)
        rows = self._conn.execute(
            f"SELECT mem_id, embedding FROM chunks WHERE {where_sql}",
            params,
        ).fetchall()
        scored = []
        for mem_id, blob in rows:
            vec = self._blob_to_vector(blob)
            scored.append((mem_id, self._cosine(query_vec, vec)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def _search_fts(self, tenant: TenantScope, query: str,
                    mem_types: list[str] | None, top_k: int,
                    filters: Optional["FilterGroup"] = None) -> list[tuple[str, float]]:
        if not self._fts_available:
            return []
        fts_query = self.build_fts_query(query)
        if not fts_query:
            return []
        user_id, scope_id = tenant.user_id, tenant.scope_id
        where_sql = "user_id=? AND scope_id=?"
        params: list = [user_id, scope_id]
        if mem_types:
            placeholders = ",".join("?" for _ in mem_types)
            where_sql += f" AND type IN ({placeholders})"
            params.extend(mem_types)
        sql_filter, sql_params = self._render_filter_group_to_sql(filters)
        if sql_filter:
            where_sql += f" AND ({sql_filter})"
            params.extend(sql_params)
        try:
            cand = self._conn.execute(
                f"SELECT rowid, mem_id FROM chunks WHERE {where_sql}", params
            ).fetchall()
            if not cand:
                return []
            rowid_to_mem = {r[0]: r[1] for r in cand}
            # Over-fetch FTS hits to compensate for post-join scalar filtering
            # (FTS LIMIT=top_k would under-return when some top-ranked FTS
            # rows belong to blacklisted memories).
            fts_limit = top_k * 4 if sql_filter else top_k
            fts_rows = self._conn.execute(
                "SELECT rowid, rank FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, fts_limit),
            ).fetchall()
            results = []
            for rowid, rank in fts_rows:
                mem_id = rowid_to_mem.get(rowid)
                if mem_id is None:
                    continue
                results.append((mem_id, self.bm25_rank_to_score(float(rank))))
            return results
        except sqlite3.Error as e:
            store_logger.warning(
                "FTS5 search failed, returning empty",
                event_type=LogEventType.STORE_RETRIEVE,
                exception=str(e),
            )
            return []

    async def search(self, tenant: TenantScope, query_vec: list[float],
                     top_k: int = 10,
                     constraints: Optional[SearchConstraints] = None,
                     ) -> list[tuple[str, float]]:
        """Hybrid vector + FTS5 search, scoped to one tenant.

        Args:
            tenant: owner scope (user_id + scope_id) — vec0 partition_key.
            query_vec: query embedding; ``[]`` degrades to FTS-only.
            top_k: max results to return after merge + sort.
            constraints: optional content-side filters packed as a
                :class:`SearchConstraints` (mem_types / query_text /
                filters). ``None`` is equivalent to "no mem_types, empty
                query_text, no filters" — the unconstrained path.
        """
        c = constraints or SearchConstraints()
        overfetch = max(top_k * _OVERFETCH_MULTIPLIER, 20)
        if self._vec_available and self._dims == len(query_vec):
            vec_hits = self._search_vec(tenant, query_vec, c.mem_types, overfetch, filters=c.filters)
        else:
            vec_hits = self._search_fallback(tenant, query_vec, c.mem_types, overfetch, filters=c.filters)
        fts_hits = self._search_fts(tenant, c.query_text, c.mem_types, overfetch, filters=c.filters) \
            if self._fts_available else []
        merged: dict[str, float] = {}
        for mid, s in vec_hits:
            merged[mid] = merged.get(mid, 0.0) + VECTOR_WEIGHT * s
        for mid, s in fts_hits:
            merged[mid] = merged.get(mid, 0.0) + FTS_WEIGHT * s
        return sorted(merged.items(), key=lambda x: x[1], reverse=True)[:top_k]

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception as e:
            store_logger.warning(
                "Failed to close SQLite connection",
                event_type=LogEventType.STORE_RETRIEVE,
                exception=str(e),
            )
