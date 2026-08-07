"""落盘实现：:class:`~storage.kv.KVStore` 的 SQLite 后端（纯标准库 ``sqlite3``）。

一张表承载所有 scope 的键值：``(org,space,user,agent,session,key)`` 为主键、``value`` 为
BLOB、``expires_at`` 为过期 Unix 秒（NULL 永不过期）。scope 各维落列做原生隔离，
``scan`` / ``scopes`` 即带 ``WHERE`` / ``DISTINCT`` 的查询，过期行读时过滤并惰性清除。

数据真正落磁盘（``db_path`` 指向文件；``":memory:"`` 为进程内 SQLite），跨进程/重启
保留。与 :class:`~storage.kv_impl.in_memory_kv_store.InMemoryKVStore` 实现同一 ``KVStore`` 契约 +
``memory_codec`` 字节，故装配时直接替换即可让真源持久化，上层无改动。

线程安全：HTTP surface 多线程，故 ``check_same_thread=False`` + 一把锁串行化访问。
"""

from __future__ import annotations

import sqlite3
import threading
import time

from common.errors import ConflictError, NotFoundError
from common.factory.factory import Factory
from common.type_def import MEMORY_KEY_PREFIX, FilterExpr, Scope
from storage.base import StoreType
from storage.kv import KvProducer, KVStore
from storage.types import KVMemoryListResult

from .memory_list import list_memory_entries

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    org TEXT NOT NULL, space TEXT NOT NULL, "user" TEXT NOT NULL,
    agent TEXT NOT NULL, session TEXT NOT NULL,
    key TEXT NOT NULL, value BLOB NOT NULL, expires_at REAL,
    PRIMARY KEY (org, space, "user", agent, session, key)
)
"""


class SQLiteKVStore(KVStore):
    """SQLite 落盘键值存储：scope 各维落列隔离，ttl 惰性过期。"""

    def __init__(self, db_path: str = "agent_memory.db") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._conn.execute(_SCHEMA)
        self._migrate_schema()

    def store_type(self) -> StoreType:
        return StoreType.KV

    def health(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1")
        return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 内部 ---------------------------------------------------------------- #

    @staticmethod
    def _expiry(ttl: float) -> float | None:
        return time.time() + ttl if ttl else None

    def _migrate_schema(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(kv)").fetchall()}
        if not columns or "space" in columns:
            return
        self._conn.execute("ALTER TABLE kv RENAME TO kv_legacy")
        self._conn.execute(_SCHEMA)
        self._conn.execute(
            'INSERT INTO kv (org, space, "user", agent, session, key, value, expires_at) '
            'SELECT org, "", "user", agent, session, key, value, expires_at FROM kv_legacy'
        )
        self._conn.execute("DROP TABLE kv_legacy")

    def _live_value(self, scope: Scope, key: str) -> bytes | None:
        """返回未过期的值；过期则惰性删除并返回 None（调用方持锁）。"""
        row = self._conn.execute(
            'SELECT value, expires_at FROM kv WHERE org=? AND space=? AND "user"=? '
            "AND agent=? AND session=? AND key=?",
            (scope.org, scope.space, scope.user, scope.agent, scope.session, key),
        ).fetchone()
        if row is None:
            return None
        value, expires_at = row
        if expires_at is not None and expires_at <= time.time():
            self._conn.execute(
                'DELETE FROM kv WHERE org=? AND space=? AND "user"=? '
                "AND agent=? AND session=? AND key=?",
                (scope.org, scope.space, scope.user, scope.agent, scope.session, key),
            )
            return None
        return bytes(value)

    # -- KVStore 契约 -------------------------------------------------------- #

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        with self._lock:
            if self._live_value(scope, key) is not None:
                raise ConflictError("kv", key)
            self._conn.execute(
                'INSERT OR REPLACE INTO kv (org,space,"user",agent,session,key,value,expires_at) '
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    scope.org,
                    scope.space,
                    scope.user,
                    scope.agent,
                    scope.session,
                    key,
                    value,
                    self._expiry(ttl),
                ),
            )

    def update(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        with self._lock:
            if self._live_value(scope, key) is None:
                raise NotFoundError("kv", key)
            self._conn.execute(
                'INSERT OR REPLACE INTO kv (org,space,"user",agent,session,key,value,expires_at) '
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    scope.org,
                    scope.space,
                    scope.user,
                    scope.agent,
                    scope.session,
                    key,
                    value,
                    self._expiry(ttl),
                ),
            )

    def delete(self, scope: Scope, key: str) -> None:
        with self._lock:
            self._conn.execute(
                'DELETE FROM kv WHERE org=? AND space=? AND "user"=? '
                "AND agent=? AND session=? AND key=?",
                (scope.org, scope.space, scope.user, scope.agent, scope.session, key),
            )

    def get(self, scope: Scope, key: str) -> bytes:
        with self._lock:
            value = self._live_value(scope, key)
        if value is None:
            raise NotFoundError("kv", key)
        return value

    def mget(self, scope: Scope, keys: List[str]) -> list[bytes]:
        # 一次 IN 查询召回命中 key → 按 keys 下标一一对应组装；任一缺失报
        # NotFoundError（与 get 一致）。与 list 同款「WHERE 过滤已过期行」语义；
        # 惰性删仍走单 key 的 _live_value/get。不去重：keys 透传。
        if not keys:
            return []
        placeholders = ",".join("?" for _ in keys)
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                f'SELECT key, value FROM kv WHERE org=? AND space=? AND "user"=? '
                "AND agent=? AND session=? AND key IN "
                f"({placeholders}) AND (expires_at IS NULL OR expires_at > ?)",
                (scope.org, scope.space, scope.user, scope.agent, scope.session, *keys, now),
            ).fetchall()
        hits = {key: bytes(value) for key, value in rows}
        out: list[bytes] = []
        for key in keys:
            value = hits.get(key)
            if value is None:
                raise NotFoundError("kv", key)
            out.append(value)
        return out

    def exists(self, scope: Scope, key: str) -> bool:
        with self._lock:
            return self._live_value(scope, key) is not None

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                'SELECT key, value FROM kv WHERE org=? AND space=? AND "user"=? '
                "AND agent=? AND session=? AND key LIKE ? "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (scope.org, scope.space, scope.user, scope.agent, scope.session, prefix + "%", now),
            ).fetchall()
        return [(key, bytes(value)) for key, value in rows]

    def list(
        self,
        scope: Scope,
        *,
        offset: int = 0,
        limit: int = 100,
        memory_types: list[str] | None = None,
        filters: FilterExpr | None = None,
        extensions: dict[str, str] | None = None,
    ) -> KVMemoryListResult:
        return list_memory_entries(
            self.scan(scope, MEMORY_KEY_PREFIX),
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extensions,
        )

    def scopes(self) -> list[Scope]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT DISTINCT org, space, "user", agent, session FROM kv'
            ).fetchall()
        return [
            Scope(
                org=org_name,
                space=space_name,
                user=user_name,
                agent=agent_name,
                session=session_name,
            )
            for org_name, space_name, user_name, agent_name, session_name in rows
        ]


# -- 注册到 KvProducer（接口层定义的工厂；实现自注册，新增无需改 producer/build_kernel） -- #


@KvProducer.register("sqlite")
def _build(config):
    return SQLiteKVStore(Factory.cfg_get(config, "db_path", "agent_memory.db"))
