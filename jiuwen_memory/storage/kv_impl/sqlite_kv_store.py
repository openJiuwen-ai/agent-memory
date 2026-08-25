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

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MEMORY_KEY_PREFIX, FilterExpr, Scope
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.kv import KvProducer, KVStore
from jiuwen_memory.storage.types import KVMemoryListResult

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
    """SQLite 落盘键值存储：scope 各维落列隔离，ttl 惰性过期。

    ``db_path`` 可经 ConfigSource ``kv_store.db_path`` 晚绑定；路径变化时关闭旧连接
    并打开新库（不迁移数据）。
    """

    def __init__(
        self,
        db_path: str = "agent_memory.db",
        *,
        config_source=None,
        config_namespace: str = "kv_store",
    ) -> None:
        """初始化 SQLiteKVStore。

        Args:
            db_path: 参数 db_path（str）。
            config_source: 参数 config_source。
            config_namespace: 参数 config_namespace（str）。
        """
        self._fallback_db_path = db_path
        self._config_source = config_source
        self._config_namespace = config_namespace
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._conn_path: str | None = None

    # -- 内部 ---------------------------------------------------------------- #

    @staticmethod
    def _expiry(ttl: float) -> float | None:
        """执行 `expiry` 操作。

        Args:
            ttl: 参数 ttl（float）。

        Returns:
            返回 float | None。
        """
        return time.time() + ttl if ttl else None

    def store_type(self) -> StoreType:
        """返回当前存储类型。

        Returns:
            返回 StoreType。
        """
        return StoreType.KV

    def health(self) -> None:
        """执行健康检查。"""
        with self._lock:
            self._ensure_conn().execute("SELECT 1")
        return None

    def close(self) -> None:
        """关闭并释放相关资源。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._conn_path = None

    # -- KVStore 契约 -------------------------------------------------------- #

    def insert(self, scope: Scope, key: str, value: bytes, ttl: float = 0.0) -> None:
        """插入一条或多条记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
            value: 参数 value（bytes）。
            ttl: 参数 ttl（float）。

        Raises:
            ConflictError: 执行失败时抛出。
        """
        with self._lock:
            self._ensure_conn()
            if self._live_value(scope, key) is not None:
                raise ConflictError("kv", key)
            conn = self._ensure_conn()
            conn.execute(
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
        """更新已有记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
            value: 参数 value（bytes）。
            ttl: 参数 ttl（float）。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        with self._lock:
            self._ensure_conn()
            if self._live_value(scope, key) is None:
                raise NotFoundError("kv", key)
            conn = self._require_conn()
            conn.execute(
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
        """删除指定的记忆或业务记录。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。
        """
        with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                'DELETE FROM kv WHERE org=? AND space=? AND "user"=? '
                "AND agent=? AND session=? AND key=?",
                (scope.org, scope.space, scope.user, scope.agent, scope.session, key),
            )

    def get(self, scope: Scope, key: str) -> bytes:
        """读取指定的记录或资源。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。

        Returns:
            返回 bytes。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        with self._lock:
            self._ensure_conn()
            value = self._live_value(scope, key)
        if value is None:
            raise NotFoundError("kv", key)
        return value

    def mget(self, scope: Scope, keys: list[str]) -> list[bytes]:
        # 一次 IN 查询召回命中 key → 按 keys 下标一一对应组装；任一缺失报
        # NotFoundError（与 get 一致）。与 list 同款「WHERE 过滤已过期行」语义；
        # 惰性删仍走单 key 的 _live_value/get。不去重：keys 透传。
        """执行 `mget` 操作。

        Args:
            scope: 参数 scope（Scope）。
            keys: 参数 keys（list[str]）。

        Returns:
            返回 list[bytes]。

        Raises:
            NotFoundError: 执行失败时抛出。
        """
        if not keys:
            return []
        placeholders = ",".join("?" for _ in keys)
        now = time.time()
        with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
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
        """检查指定记录或资源是否存在。

        Args:
            scope: 参数 scope（Scope）。
            key: 参数 key（str）。

        Returns:
            返回 bool。
        """
        with self._lock:
            self._ensure_conn()
            return self._live_value(scope, key) is not None

    def scan(self, scope: Scope, prefix: str = "") -> list[tuple[str, bytes]]:
        """扫描指定范围内的记录。

        Args:
            scope: 参数 scope（Scope）。
            prefix: 参数 prefix（str）。

        Returns:
            返回 list[tuple[str, bytes]]。
        """
        now = time.time()
        with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
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
        """列出符合条件的记录或资源。

        Args:
            scope: 参数 scope（Scope）。
            offset: 参数 offset（int）。
            limit: 参数 limit（int）。
            memory_types: 参数 memory_types（list[str] | None）。
            filters: 参数 filters（FilterExpr | None）。
            extensions: 参数 extensions（dict[str, str] | None）。

        Returns:
            返回 KVMemoryListResult。
        """
        return list_memory_entries(
            self.scan(scope, MEMORY_KEY_PREFIX),
            offset=offset,
            limit=limit,
            memory_types=memory_types,
            filters=filters,
            extensions=extensions,
        )

    def scopes(self) -> list[Scope]:
        """执行 `scopes` 操作。

        Returns:
            返回 list[Scope]。
        """
        with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
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

    def _resolved_db_path(self) -> str:
        """解析并返回目标配置或资源。

        Returns:
            返回 str。
        """
        from jiuwen_memory.config.binding import resolve_connection_url

        live = resolve_connection_url(
            self._config_source,
            namespace=self._config_namespace,
            field="db_path",
            fallback=self._fallback_db_path,
        )
        return live or self._fallback_db_path

    def _ensure_conn(self) -> sqlite3.Connection:
        """按当前 ``db_path`` 惰性打开连接；路径变化则重建。调用方须已持 ``_lock``。"""
        path = self._resolved_db_path()
        if self._conn is not None and self._conn_path == path:
            return self._conn
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._conn_path = None
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn_path = path
        self._conn.execute(_SCHEMA)
        self._migrate_schema(self._conn)
        return self._conn

    def _require_conn(self) -> sqlite3.Connection:
        """返回已打开连接；调用方须已持锁且刚调用过 ``_ensure_conn``。"""
        conn = self._conn
        if conn is None:
            raise RuntimeError("SQLiteKVStore connection is not open")
        return conn

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """执行 `migrate_schema` 操作。

        Args:
            conn: 参数 conn（sqlite3.Connection）。
        """
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(kv)").fetchall()
        }
        if not columns or "space" in columns:
            return
        conn.execute("ALTER TABLE kv RENAME TO kv_legacy")
        conn.execute(_SCHEMA)
        conn.execute(
            'INSERT INTO kv (org, space, "user", agent, session, key, value, expires_at) '
            'SELECT org, "", "user", agent, session, key, value, expires_at FROM kv_legacy'
        )
        conn.execute("DROP TABLE kv_legacy")

    def _live_value(self, scope: Scope, key: str) -> bytes | None:
        """返回未过期的值；过期则惰性删除并返回 None（调用方持锁且已 ensure_conn）。"""
        conn = self._require_conn()
        row = conn.execute(
            'SELECT value, expires_at FROM kv WHERE org=? AND space=? AND "user"=? '
            "AND agent=? AND session=? AND key=?",
            (scope.org, scope.space, scope.user, scope.agent, scope.session, key),
        ).fetchone()
        if row is None:
            return None
        value, expires_at = row
        if expires_at is not None and expires_at <= time.time():
            conn.execute(
                'DELETE FROM kv WHERE org=? AND space=? AND "user"=? '
                "AND agent=? AND session=? AND key=?",
                (scope.org, scope.space, scope.user, scope.agent, scope.session, key),
            )
            return None
        return bytes(value)


# -- 注册到 KvProducer（接口层定义的工厂；实现自注册，新增无需改 producer/build_kernel） -- #


@KvProducer.register("sqlite")
def _build(config):
    """根据配置构建组件实例。

    Args:
        config: 参数 config。
    """
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    return SQLiteKVStore(
        Factory.cfg_get(config, "db_path", "agent_memory.db"),
        config_source=ConfigSourceProducer.get_cached("default"),
    )
