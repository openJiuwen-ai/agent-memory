"""影子索引的 SQLite + sqlite-vec 实现（支持降级）。

一个 sqlite 文件承载三张表

- ``memory_unit``：全量 + 按 key 查（普通表，``unit_id`` TEXT 主键 + ``unit_json`` BLOB）
- ``memory_fts``：FTS5 倒排表（普通 FTS5 表，自存 token 串），靠隐式 ``rowid`` 关联回 ``memory_unit``
- ``memory_vec``：sqlite-vec ``vec0`` 向量表，``rowid`` 与 ``memory_unit`` 对齐

三表同库、同连接、同事务写入，靠 ``unit_id`` / 隐式 ``rowid`` 关联。

**降级机制**）：``embedder`` 与 ``sqlite_vec`` 均为可选依赖。

- **完整模式**（embedder 注入 + sqlite_vec 可加载）：三表全建，``insert_units`` 写全量+倒排+向量，
  ``search_fulltext``/``search_vector`` 均可用。
- **降级模式**（embedder 未注入 或 sqlite_vec 不可用）：只建 ``memory_unit`` + ``memory_fts`` 两表，
  跳过 ``memory_vec`` 建表与写入；``search_fulltext`` 正常工作，``search_vector`` 返回空列表
  （不抛错，优雅降级——让上层检索编排照常运行，只是向量召回缺一路）。

连接管理对齐 :class:`~storage.kv_impl.sqlite_kv_store.SQLiteKVStore`。与 KVStore 的差异：
``sqlite_vec`` 是运行时加载扩展，完整模式下 ``_ensure_conn`` 在 ``connect`` 后、建表前
``enable_load_extension(True) + sqlite_vec.load(conn)``，且连接重开时每次都要重新 load
（否则查 vec0 报 ``no such module: vec0``）。降级模式下跳过此步。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from datetime import datetime

from jiuwen_memory.common.embedder.base import Embedder, EmbedderProducer
from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.tokenizer.base import Tokenizer, TokenizerProducer
from jiuwen_memory.common.type_def import (
    COORDS_KEY,
    FilterClause,
    FilterGroup,
    FilterLogic,
    FilterOp,
    MD_FILENAME_KEY,
    MEMORY_CLASS_KEY,
    MemoryUnit,
    Scope,
    T_EVENT_UNKNOWN,
    T_INVALID_OPEN,
    iter_clauses,
)
from jiuwen_memory.common.type_def.memory_codec import dumps, loads
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.shadow import DocumentShadowIndex, ShadowIndexProducer
from jiuwen_memory.storage.types import ScoredID, TextQuery, VectorQuery

_DEFAULT_PROJECT = "default"
_DEFAULT_CLASS = "team_memory"
_DEFAULT_LIFECYCLE = "active"  # memory_unit.lifecycle 默认值（= LifecycleState.ACTIVE.value，绝大多数 unit 是 ACTIVE）
_DEFAULT_OVERSAMPLE = 4  # vec0 向量召回批 1 过采样倍数（post-filter 召回不足兜底，F07 §4.4.3）

# project 作为配置声明的收窄维，coords 折算后以 ``system_metadata.project`` 谓词
# 下推（``IN ["", <value>]``，空串兜底：该维不适用/判否的条目一并命中，见
# ``space_predicates._narrow_predicates``）。召回侧从此谓词取 project 值，
# 而非从 Scope（团队记忆根本不走 scope 字段，F08 §1.2 复用 coords）。
_PROJECT_FILTER_FIELD = "system_metadata.project"

# 召回分两批的 category 集合（F08 §5 复用 memory_class）：
#   批 1（受 project 隔离）：project_memory + team_memory
#   批 2（全局可见，无 project 限制）：user_memory
_PROJECT_SCOPED_CLASSES = ("project_memory", "team_memory")
_GLOBAL_CLASS = "user_memory"


def _try_import_sqlite_vec():
    """尝试导入 sqlite_vec；失败返回 None（降级模式）。"""
    try:
        import sqlite_vec

        return sqlite_vec
    except ImportError:
        return None


def _build_schema(vec_dim: int | None) -> str:
    """生成建表 DDL。

    ``vec_dim`` 为 None 时（降级模式：embedder 未注入或 sqlite_vec 不可用），
    只建 ``memory_unit`` + ``memory_fts`` 两表，跳过 ``memory_vec``——
    降级场景下向量召回缺一路，倒排与全量存储照常。

    ``memory_unit.category`` 列名保留（不改表结构），但 F08 复用 memory_class 后落的
    是 memory_class 值（如 ``team_memory``），默认值取 F08 §2 兜底 ``team_memory``——
    非 F07 原设计的 ``diary``。

    FTS5 ``tokenize``： ``'simple'``（"不二次分词，分词归 jieba 预处理"），
    但当前运行环境的 SQLite 不含 ``simple`` tokenizer（仅 ``unicode61``/``ascii``/``porter``）。
    改用 ``'unicode61'``——写入前已用注入 tokenizer 预分词成空格分隔 token 串，
    unicode61 按空格切分即还原 token，满足"FTS5 只管倒排结构、分词归预处理器"的设计意图
    （见 F08 §4 步骤4 待定项4 修订）。

    FTS5 external content 模式： 用 ``content='memory_unit'`` 关联回 memory_unit
    以「不重复存正文」。但 external content 要求关联表有同名 ``content`` 列，而 memory_unit
    无此列（只有 ``unit_json``），导致任何解析 content 列的查询（含 ``count(*)``/MATCH）报
    ``no such column: T.content``。改用**普通 FTS5 表**（自存 token 串），靠 INSERT 时显式
    给相同 ``rowid`` 与 memory_unit 对齐——代价是 token 串存两份（FTS5 一份 + unit_json 内含
    原 content），但 token 串非原正文体积可控，且消除列名约束的运维负担
    （见 F08 §4 步骤4 修订）。
    """
    ddl = f"""
CREATE TABLE IF NOT EXISTS memory_unit (
    unit_id      TEXT PRIMARY KEY,
    content_hash TEXT,
    md_filename  TEXT,
    project      TEXT NOT NULL DEFAULT '{_DEFAULT_PROJECT}',
    category     TEXT NOT NULL DEFAULT '{_DEFAULT_CLASS}',
    lifecycle    TEXT NOT NULL DEFAULT '{_DEFAULT_LIFECYCLE}',
    t_valid      INTEGER,
    t_invalid    INTEGER,
    t_event      INTEGER,
    unit_json    BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_hash ON memory_unit(content_hash);
CREATE INDEX IF NOT EXISTS idx_md_filename ON memory_unit(md_filename);
CREATE INDEX IF NOT EXISTS idx_project_category ON memory_unit(project, category);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    tokenize = 'unicode61'
);
"""
    if vec_dim is not None:
        ddl += f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
    embedding float[{vec_dim}]
);
"""
    return ddl


def _epoch_ms(value: datetime | None) -> int | None:
    """datetime → epoch 毫秒；None 保持 None（与 ``memory_filter._epoch_ms`` 同口径）。"""
    return None if value is None else int(value.timestamp() * 1000)


def _lifecycle_of(unit: MemoryUnit) -> str:
    return unit.lifecycle.value


def _t_valid_of(unit: MemoryUnit) -> int | None:
    return _epoch_ms(unit.temporal.t_valid)


def _t_invalid_of(unit: MemoryUnit) -> int:
    """``t_invalid=None`` → 哨兵 ``T_INVALID_OPEN``（与 ``memory_filter._field_value`` 投影对称）。"""
    v = _epoch_ms(unit.temporal.t_invalid)
    return T_INVALID_OPEN if v is None else v


def _t_event_of(unit: MemoryUnit) -> int:
    """``t_event=None`` → 哨兵 ``T_EVENT_UNKNOWN``（与 ``memory_filter._field_value`` 投影对称）。"""
    v = _epoch_ms(unit.temporal.t_event)
    return T_EVENT_UNKNOWN if v is None else v


# 系统前置谓词（build_system_filters 产出）→ memory_unit 投影列映射。
# 与 ``_projects_from_filters`` 分工：后者从 filters 取 project 隔离值做分批下推，
# 这里把 lifecycle/t_valid/t_invalid/t_event 编译成 SQL WHERE 片段下推——对齐非文档
# 流程「先排除什么在索引级生效」的前置谓词下推（predicate_builder.build_system_filters），
# 点读后 ``is_retrieval_candidate`` 仍作纵深防御兜底。
_SYSTEM_PREDICATE_COLUMNS = {
    "lifecycle": "lifecycle",
    "t_valid": "t_valid",
    "t_invalid": "t_invalid",
    "t_event": "t_event",
}


def _compile_clause(col: str, op: FilterOp, value) -> tuple[str | None, list]:
    """把单个系统叶子谓词编译成 ``列 op ?``；不可编译的算子返回 None。"""
    if op is FilterOp.EQ:
        return f"{col} = ?", [value]
    if op is FilterOp.NE:
        return f"{col} != ?", [value]
    if op is FilterOp.IN:
        ph = ",".join("?" for _ in value)
        return f"{col} IN ({ph})", list(value)
    if op is FilterOp.NOT_IN:
        ph = ",".join("?" for _ in value)
        return f"{col} NOT IN ({ph})", list(value)
    if op is FilterOp.GT:
        return f"{col} > ?", [value]
    if op is FilterOp.GTE:
        return f"{col} >= ?", [value]
    if op is FilterOp.LT:
        return f"{col} < ?", [value]
    if op is FilterOp.LTE:
        return f"{col} <= ?", [value]
    return None, []  # CONTAINS 等对系统字段无意义


def _compile_system_filters(expr) -> tuple[str | None, list]:
    """把系统谓词（lifecycle/t_valid/t_invalid/t_event）编译成 SQL WHERE 片段。

    递归遍历 ``FilterExpr`` 树：系统字段叶子 → ``列 op ?``；非系统字段叶子（project、
    用户 filters）→ 返回 None 表示「该分支无约束」（由点读后复核兜底）。AND 组跳过
    None child；OR 组任一 child 为 None 则整体放弃（含恒真 child → 整体恒真，不可下推）。
    返回 ``(sql, params)``，``sql`` 为 None 表示无系统谓词可下推。
    """
    if expr is None:
        return None, []
    if isinstance(expr, FilterClause):
        col = _SYSTEM_PREDICATE_COLUMNS.get(expr.field)
        if col is None:
            return None, []
        return _compile_clause(col, expr.op, expr.value)
    if expr.logic is FilterLogic.AND:
        parts: list[str] = []
        params: list = []
        for child in expr.children:
            c_sql, c_params = _compile_system_filters(child)
            if c_sql is None:
                continue
            parts.append(f"({c_sql})")
            params.extend(c_params)
        if not parts:
            return None, []
        return " AND ".join(parts), params
    if expr.logic is FilterLogic.OR:
        parts = []
        params = []
        for child in expr.children:
            c_sql, c_params = _compile_system_filters(child)
            if c_sql is None:
                return None, []  # OR 组含无约束 child → 整体恒真，放弃下推
            parts.append(f"({c_sql})")
            params.extend(c_params)
        if not parts:
            return None, []
        return " OR ".join(parts), params
    # NOT：build_system_filters 不产生系统 NOT 谓词，放弃下推（点读后复核兜底）。
    return None, []


def _ensure_system_columns(conn: sqlite3.Connection) -> None:
    """迁移：旧版 ``memory_unit`` 表缺 lifecycle/t_valid/t_invalid/t_event 投影列时补齐。

    F08 实现期 schema 演进——``CREATE TABLE IF NOT EXISTS`` 不会给已存在的旧表加列，
    故用 ``PRAGMA table_info`` 探测 + ``ALTER TABLE ADD COLUMN`` 幂等补齐，不丢已写入
    unit 数据（对比 DROP 重建）。NOT NULL 列（lifecycle）须带默认值（SQLite 约束）。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_unit)")}
    if "lifecycle" not in cols:
        conn.execute(
            "ALTER TABLE memory_unit ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'active'"
        )
    if "t_valid" not in cols:
        conn.execute("ALTER TABLE memory_unit ADD COLUMN t_valid INTEGER")
    if "t_invalid" not in cols:
        conn.execute("ALTER TABLE memory_unit ADD COLUMN t_invalid INTEGER")
    if "t_event" not in cols:
        conn.execute("ALTER TABLE memory_unit ADD COLUMN t_event INTEGER")


class SqliteDocumentShadowIndex(DocumentShadowIndex):
    """文档场景影子索引的 SQLite + sqlite-vec 实现（支持降级）。

    构造期注入 ``embedder``（可选）+ ``tokenizer``（必填），``db_path`` 经 ConfigSource
    晚绑定，解析优先级见 :meth:`_resolved_db_path`（显式 ``shadow_index.db_path`` >
    ``markdown_store.root`` 派生 > 构造期 fallback）。连接惰性打开，路径变化则重建
    （不迁移数据）。

    降级判定（``_vec_enabled``）：embedder 为 None **或** sqlite_vec 不可导入/load 失败 →
    降级模式，不建 ``memory_vec`` 表、不写向量、``search_vector`` 返回空列表。
    """

    def __init__(
        self,
        db_path: str | None = None,
        *,
        embedder: Embedder | None = None,
        tokenizer: Tokenizer,
        config_source=None,
        config_namespace: str = "shadow_index",
        markdown_root: str | None = None,
    ) -> None:
        self._fallback_db_path = db_path
        self._embedder = embedder
        self._tokenizer = tokenizer
        self._config_source = config_source
        self._config_namespace = config_namespace
        # 装配期从 build params 跨 namespace 读到的 markdown_store.root（供 db_path 派生）。
        # 运行时 _resolved_db_path 优先查 config_source 晚绑定的 markdown root，查不到回退此值。
        self._fallback_markdown_root = markdown_root
        # sqlite_vec 模块句柄；None 表示不可用（降级模式）。构造期尝试导入，运行期 load
        # 失败时也会置 None（_ensure_conn 内 try）。embedder 为 None 时直接降级，不导入。
        self._sqlite_vec = _try_import_sqlite_vec() if embedder is not None else None
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._conn_path: str | None = None
        # 运行期标记：连接建立后 sqlite_vec.load 是否成功。load 失败 → 降级（即使模块能导入）。
        self._vec_loaded = False

    # -- 降级判定 ------------------------------------------------------------ #

    @property
    def vec_enabled(self) -> bool:
        """向量召回是否可用（完整模式）。False 表示降级模式（无 memory_vec 表）。"""
        return self._embedder is not None and self._sqlite_vec is not None and self._vec_loaded

    # -- 内部 ---------------------------------------------------------------- #

    def store_type(self) -> StoreType:
        return StoreType.DOCUMENT_SHADOW

    def health(self) -> None:
        with self._lock:
            self._ensure_conn().execute("SELECT 1")
        return None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._conn_path = None

    def _resolved_db_path(self) -> str:
        """解析 db_path：

        1. ``shadow_index.db_path`` 显式配置（最高优先级，覆盖一切）
        2. ``markdown_store.root`` 派生：``{markdown_root}/.shadow/shadow.db``
           （md 与影子索引同根伴生，影子索引是 md 的机器索引，物理位置应跟随 md 根目录）
        3. 构造期 fallback（兜底，当 markdown root 也未配置时）

        优先级 2 让 db 自动跟随 markdown 根目录移动而无需重复配置——
        用户只配 ``markdown_store.root`` 一处，shadow db 自动落其下 ``.shadow/`` 子目录。
        """
        from jiuwen_memory.config.binding import resolve_connection_url

        # 1. 显式 db_path：优先 config_source 晚绑定，回退构造期 build params 的 db_path。
        #    构造期 fallback 为 None 时表示未显式配（_build 用 cfg_get 读不到 db_path 传 None），
        #    走第 2 级 markdown_root 派生。
        live = resolve_connection_url(
            self._config_source,
            namespace=self._config_namespace,
            field="db_path",
            fallback=self._fallback_db_path,
        )
        if live:
            return live
        # 2. 未显式配 db_path → 从 markdown_store.root 派生
        # 优先 config_source 晚绑定的 markdown root（生产环境 root 可能来自外部配置中心），
        # 查不到回退装配期从 build params 跨 namespace 读到的 root
        markdown_root = resolve_connection_url(
            self._config_source,
            namespace="markdown_store",
            field="root",
            fallback=self._fallback_markdown_root,
        )
        if markdown_root:
            return os.path.join(markdown_root, ".shadow", "shadow.db")
        # markdown root 也未配 → cwd 下 .shadow/shadow.db 兜底（极少见：未配 markdown_store）
        return self._fallback_db_path or ".shadow/shadow.db"

    def _ensure_conn(self) -> sqlite3.Connection:
        """按当前 ``db_path`` 惰性打开连接；路径变化则重建。调用方须已持 ``_lock``。

        降级处理：embedder 为 None 或 sqlite_vec 不可用/load 失败时，
        跳过 vec0 建表与向量写入，只建 memory_unit + memory_fts。完整模式下与
        SQLiteKVStore 的差异：开连接后、建表前必须
        ``enable_load_extension(True) + sqlite_vec.load(conn)``，且连接重开时每次都要重新 load
        （否则查 vec0 报 ``no such module: vec0``）。
        另：默认 db_path 派生自 ``markdown_store.root`` 的 ``{root}/.shadow/shadow.db``，含子目录，连接前需确保父目录存在。
        """
        path = self._resolved_db_path()
        if self._conn is not None and self._conn_path == path:
            return self._conn
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._conn_path = None
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn = conn
        self._conn_path = path

        # 尝试加载 sqlite_vec（完整模式）；失败则降级（不建 memory_vec 表）。
        # embedder 为 None 时 _sqlite_vec 已是 None，直接降级，连 load 都不试。
        vec_dim: int | None = None
        self._vec_loaded = False
        if self._sqlite_vec is not None and self._embedder is not None:
            try:
                conn.enable_load_extension(True)
                self._sqlite_vec.load(conn)
                self._vec_loaded = True
                vec_dim = self._embedder.dimension()
            except Exception:
                # load 失败（环境不支持 vec0 扩展）→ 降级，只建两表
                self._vec_loaded = False
                vec_dim = None
        # _build_schema(vec_dim=None) 时跳过 memory_vec DDL；executescript 一次跑完
        conn.executescript(_build_schema(vec_dim))
        # 迁移：旧表缺 lifecycle/t_valid/t_invalid/t_event 投影列时幂等补齐（F08 实现期演进）
        _ensure_system_columns(conn)
        return conn

    def _require_conn(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            raise RuntimeError("SqliteDocumentShadowIndex connection is not open")
        return conn

    # -- 静态派生 ------------------------------------------------------------ #

    @staticmethod
    def _content_of(unit: MemoryUnit) -> str:
        """取 ``segments[0].content`` 作正文（F07 §11.2/§11.5）。"""
        segs = unit.segments or []
        return segs[0].content if segs else ""

    @staticmethod
    def _content_hash(content: str) -> str:
        """``content`` 整段 sha256，不归一化（F08 §4 步骤4 待定项3，取最简）。"""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _project_of(unit: MemoryUnit) -> str:
        """从 ``coords`` 读 project，空落 ``default``（F08 §1.2/§3）。"""
        coords = (unit.system_metadata or {}).get(COORDS_KEY) or {}
        return coords.get("project") or _DEFAULT_PROJECT

    @staticmethod
    def _projects_from_filters(filters) -> list[str]:
        """从召回谓词取 project 隔离值（F08 §1.2 复用 coords）。

        上层（``local_memory_api`` → ``narrow_dims_of`` → ``_narrow_predicates``）
        已把 coords 折算成 ``system_metadata.project IN ["", <value>]`` 谓词下推。
        本方法从 ``query.filters`` 深度遍历收集该字段的全部取值，**过滤掉空串**
        （空串是「该维不适用」的兜底项，``memory_unit.project`` 列不会存空串——
        写入侧 ``_project_of`` 空落 ``default``，故保留空串无意义），去重后返回。

        无 project 谓词（调用方未带 coords、或该维未收窄）→ 返回 ``[_DEFAULT_PROJECT]``，
        等价于不按 project 隔离——失效方向是放宽，与团队记忆坐标缺项语义一致。
        """
        values: list[str] = []
        seen: set[str] = set()
        for clause in iter_clauses(filters):
            if clause.field != _PROJECT_FILTER_FIELD:
                continue
            raw = clause.value
            items = raw if isinstance(raw, (list, tuple)) else [raw]
            for item in items:
                v = str(item or "").strip()
                if v and v not in seen:
                    seen.add(v)
                    values.append(v)
        if not values:
            return [_DEFAULT_PROJECT]
        return values

    @staticmethod
    def _category_of(unit: MemoryUnit) -> str:
        """落 memory_class 值，空落 ``team_memory``（F08 §2/§5）。

        md.write 已在写入 md 前兜底回填 memory_class（F08 §4 步骤6 关键细节），
        故此处一般非空；留兜底是为防直接调 ``insert_units``（不经 md.write）的场景。
        """
        metadata = unit.system_metadata or {}
        return metadata.get(MEMORY_CLASS_KEY) or _DEFAULT_CLASS

    @staticmethod
    def _md_filename_of(unit: MemoryUnit) -> str:
        """从 ``system_metadata[MD_FILENAME_KEY]`` 读 md 相对路径（由 md.write 回填，F07 §11.3）。"""
        return (unit.system_metadata or {}).get(MD_FILENAME_KEY) or ""

    # -- DocumentShadowIndex 契约 ------------------------------------------- #

    def insert_units(self, scope: Scope, units: list[MemoryUnit]) -> None:
        with self._lock:
            conn = self._ensure_conn()
            try:
                conn.execute("BEGIN")
                for unit in units:
                    self._insert_one(conn, unit)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _insert_one(self, conn: sqlite3.Connection, unit: MemoryUnit) -> None:
        # 冲突预检：unit_id 已存在报 ConflictError（对齐 KVStore.insert 语义，F07 §11.3）
        existing = conn.execute(
            "SELECT 1 FROM memory_unit WHERE unit_id=?", (unit.id,)
        ).fetchone()
        if existing is not None:
            raise ConflictError("shadow", unit.id)

        content = self._content_of(unit)
        content_hash = self._content_hash(content)
        md_filename = self._md_filename_of(unit)
        project = self._project_of(unit)
        category = self._category_of(unit)
        lifecycle = _lifecycle_of(unit)
        t_valid = _t_valid_of(unit)
        t_invalid = _t_invalid_of(unit)
        t_event = _t_event_of(unit)
        unit_json = dumps(unit)

        # ① memory_unit（拿隐式 rowid，FTS5/vec0 靠它关联，F07 §11.2 注 A）
        conn.execute(
            "INSERT INTO memory_unit "
            "(unit_id, content_hash, md_filename, project, category, "
            "lifecycle, t_valid, t_invalid, t_event, unit_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (unit.id, content_hash, md_filename, project, category,
             lifecycle, t_valid, t_invalid, t_event, unit_json),
        )
        row = conn.execute(
            "SELECT rowid FROM memory_unit WHERE unit_id=?", (unit.id,)
        ).fetchone()
        rowid = row[0]

        # ② memory_fts：注入 tokenizer 预分词成 token 串，FTS5 tokenize='simple' 不二次分词（F07 §11.5）
        tokens = self._tokenizer.tokenize(content)
        fts_content = " ".join(tokens)
        conn.execute(
            "INSERT INTO memory_fts (rowid, content) VALUES (?,?)",
            (rowid, fts_content),
        )

        # ③ memory_vec：embedder 向量化 content，vec0 rowid 与 memory_unit 对齐。
        # 降级模式（embedder 为 None 或 vec 未加载）跳过向量写入，只留倒排+全量。
        if self.vec_enabled:
            vec = self._embedder.embed([content])[0]
            conn.execute(
                "INSERT INTO memory_vec (rowid, embedding) VALUES (?,?)",
                (rowid, _vec_to_blob(vec)),
            )

    # -- 查询类：get_units/update_units/delete_units/list_units/list_units_by_md 已全部落地 -- #

    def get_units(self, scope: Scope, unit_ids: list[str]) -> list[MemoryUnit]:
        """按 ``unit_id`` 点查全量 ``MemoryUnit``（F07 §11.3 / F08 §5.1）。

        ``scope`` 当前不参与过滤——影子索引靠 project+category 隔离（§5 已定，
        不走 Scope 字段），scope 仅作签名占位对齐契约（与 ``insert_units`` 一致：
        收着但写入不落 scope 列）。

        **缺失 id 省略（不抛 NotFoundError）**：与 :class:`~storage.kv.KVStore.get`
        「缺失即报错」是刻意差异——影子索引点查服务召回侧 materialize（§4.2），
        命中的 ``unit_id`` 来自 ``search_fulltext``/``search_vector`` 的 ``ScoredID``,
        若中途被删应静默跳过而非整批失败。返回列表按下标对应**输入顺序**组装
        （与 ``KVStore.mget`` 保序一致），便于调用方按下标对位。
        """
        if not unit_ids:
            return []
        with self._lock:
            conn = self._ensure_conn()
            placeholders = ",".join("?" for _ in unit_ids)
            rows = conn.execute(
                f"SELECT unit_id, unit_json FROM memory_unit "
                f"WHERE unit_id IN ({placeholders})",
                tuple(unit_ids),
            ).fetchall()
        # 命中 id → MemoryUnit；非 dict/无 id 的 unit_json 经 loads 归 None，过滤掉
        # （memory_codec.loads 对非 MemoryUnit 字节返回 None，见该模块容错演进说明）。
        hits: dict[str, MemoryUnit | None] = {}
        for uid, raw in rows:
            unit = loads(bytes(raw))
            if unit is not None:
                hits[uid] = unit
        # 按输入顺序保序组装，缺失/非法的下标直接省略（不补 None，不抛错）。
        return [hits[uid] for uid in unit_ids if uid in hits]

    def update_units(self, scope: Scope, units: list[MemoryUnit]) -> None:
        """覆写全量 ``unit_json``，按 ``content_hash`` 变化判定投影是否重建（F07 §11.3）。

        id 不存在报 :class:`NotFoundError`（对齐 :meth:`KVStore.update`）。``scope`` 不参与
        过滤（与 insert/get/list 同口径，靠 project+category 隔离）。

        **投影重建规则（§11.3 契约，§5.2.1/§5.2.2 两场景）**：
        - content_hash 变（content 改，如 §5.2.1 OVERWRITE）→ 重建 FTS5 倒排 + vec0 向量
          （jieba 重分词重排 + 重 embed）；
        - content_hash 未变（只改状态字段，如 §5.2.2 SUPERSEDE 旧版设 t_invalid/lifecycle）
          → 只覆写 ``unit_json``，**不重建投影**（content 没变，重排倒排/重 embed 是浪费）。

        算子内部先 SELECT 旧 content_hash 比对，再决定投影是否重建——这天然支持上层
        ``mode=FORWARD_ONLY`` 调用（仅改状态字段时 content_hash 不变，自动走「只覆写
        unit_json」路径，正是 FORWARD_ONLY 期望的「检索索引不动」语义，无需 update_units
        自身感知 mode）。

        重建走「DELETE 旧行 + INSERT 新行」而非 UPDATE 原地改——FTS5/vec0 虚表对原地
        UPDATE 支持不稳（vec0 无 UPDATE 语义，FTS5 原地 UPDATE 会触发整行重索引但行为
        依赖实现版本），显式 DELETE+INSERT 语义清晰、跨版本一致（与 delete_units 同范式，
        §12.7「external content 不级联，必须双写」）。
        """
        with self._lock:
            conn = self._ensure_conn()
            try:
                conn.execute("BEGIN")
                for unit in units:
                    self._update_one(conn, unit)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _update_one(self, conn: sqlite3.Connection, unit: MemoryUnit) -> None:
        # 旧行定位：id 不存在报缺失（对齐 KVStore.update 语义，F07 §11.3）。
        # project/md_filename/category 一并取出，供下方「空兜底守卫」保留旧列值。
        row = conn.execute(
            "SELECT rowid, content_hash, project, md_filename, category "
            "FROM memory_unit WHERE unit_id=?",
            (unit.id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("shadow", unit.id)
        rowid, old_hash, old_project, old_md_filename, old_category = row

        content = self._content_of(unit)
        new_hash = self._content_hash(content)
        md_filename = self._md_filename_of(unit)
        project = self._project_of(unit)
        category = self._category_of(unit)
        # 投影列空兜底守卫：coords 是 TRANSIENT 键（dumps 剥除，memory_codec），
        # 从影子索引读回的 unit 永远没有 coords → _project_of 落 "default"。若不
        # 守卫，任何 read-modify-write 循环（SUPERSEDE 改 lifecycle、dedup UPDATE
        # 合并、LifecycleManager.transition/sweep——unit 都取自读回）都会把原
        # project="p_xxx" 的行重置为 default，下次按 project 隔离召回即丢失。
        # 取到兜底值时保留旧列值：project 是写入时确定的归属坐标（F07 语义下
        # 不可变），读到兜底只可能是"上游丢了瞬态上下文"而非"调用方想改归属"。
        # md_filename 同理（md.write 回填的内部指针，重建 unit 对象的上游可能
        # 丢键，覆写空串会让 list_units_by_md 的看门狗对账失锚）；category 同理
        # （memory_class 是落盘键、读回保留，取到兜底说明上游重建对象丢了键）。
        # lifecycle/t_valid/t_invalid/t_event 不守卫——它们是 update 的语义本体
        # （SUPERSEDE 改的就是它们），且 t_valid=None 是合法值非兜底。
        if project == _DEFAULT_PROJECT and old_project:
            project = old_project
        if not md_filename and old_md_filename:
            md_filename = old_md_filename
        if category == _DEFAULT_CLASS and old_category:
            category = old_category
        lifecycle = _lifecycle_of(unit)
        t_valid = _t_valid_of(unit)
        t_invalid = _t_invalid_of(unit)
        t_event = _t_event_of(unit)
        unit_json = dumps(unit)

        # ① memory_unit 全字段覆写（unit_json + content_hash + md_filename + project + category
        # + lifecycle/t_valid/t_invalid/t_event 投影列，rowid 不变——FTS5/vec0 靠 rowid 关联，
        # 换 rowid 会断链）。投影列无论 content_hash 是否变都覆写：SUPERSEDE 旧版只改
        # lifecycle/t_invalid（content_hash 不变），投影列仍须同步（谓词下推依赖它们）。
        conn.execute(
            "UPDATE memory_unit "
            "SET content_hash=?, md_filename=?, project=?, category=?, "
            "lifecycle=?, t_valid=?, t_invalid=?, t_event=?, unit_json=? "
            "WHERE unit_id=?",
            (new_hash, md_filename, project, category,
             lifecycle, t_valid, t_invalid, t_event, unit_json, unit.id),
        )

        # ② 投影重建仅当 content_hash 变（content 改）。content_hash 未变（只改状态字段）
        # → 跳过投影重建，只覆写 unit_json（上面已 UPDATE），与 §11.3 契约一致。
        if new_hash == old_hash:
            return

        # FTS5 重建：删旧行再插新行（token 串重分词，F07 §11.5）。
        conn.execute("DELETE FROM memory_fts WHERE rowid=?", (rowid,))
        tokens = self._tokenizer.tokenize(content)
        fts_content = " ".join(tokens)
        conn.execute(
            "INSERT INTO memory_fts (rowid, content) VALUES (?,?)",
            (rowid, fts_content),
        )

        # vec0 重建：仅完整模式（降级模式无 memory_vec 表，vec_enabled=False，跳过）。
        if self.vec_enabled:
            conn.execute("DELETE FROM memory_vec WHERE rowid=?", (rowid,))
            vec = self._embedder.embed([content])[0]
            conn.execute(
                "INSERT INTO memory_vec (rowid, embedding) VALUES (?,?)",
                (rowid, _vec_to_blob(vec)),
            )

    def delete_units(self, scope: Scope, unit_ids: list[str]) -> None:
        """按 ``unit_id`` 删全量 + 投影（幂等），同事务显式删三表（F07 §11.3）。

        **幂等**：删不存在的 id 静默跳过（不报 NotFoundError），与 :meth:`KVStore.delete`
        「缺失不报错」对齐——delete 语义是「确保不存在」，非「必须已存在」。

        **同事务显式删三表**（§12.7「external content 不级联，必须双写」）：memory_fts /
        memory_vec 与 memory_unit 靠隐式 rowid 关联、无外键/触发器级联，删 memory_unit 行
        不会自动清理投影行，故必须显式删。顺序：先删投影（fts/vec，靠 rowid），再删主表
        （靠 unit_id）——先查 rowid 再删，两段独立、顺序不影响结果，但先投影后主表语义清晰。

        ``scope`` 不参与过滤（与 insert/get/update/list 同口径，靠 project+category 隔离）。

        **降级模式守卫**：``memory_fts`` 降级/完整模式都建（两表模式），无守卫；``memory_vec``
        仅完整模式建（``vec_enabled=True`` 才有表），降级模式 ``DELETE FROM memory_vec`` 会
        报「no such table」，故 ``vec_enabled`` 守卫跳过。
        """
        with self._lock:
            conn = self._ensure_conn()
            if not unit_ids:
                return
            try:
                conn.execute("BEGIN")
                placeholders = ",".join("?" for _ in unit_ids)
                # 先查存在的 rowid（幂等：只删存在的 id，缺失静默跳过）。
                rows = conn.execute(
                    f"SELECT rowid FROM memory_unit WHERE unit_id IN ({placeholders})",
                    tuple(unit_ids),
                ).fetchall()
                rowids = [r[0] for r in rows]
                if rowids:
                    rowid_ph = ",".join("?" for _ in rowids)
                    conn.execute(
                        f"DELETE FROM memory_fts WHERE rowid IN ({rowid_ph})",
                        tuple(rowids),
                    )
                    if self.vec_enabled:
                        conn.execute(
                            f"DELETE FROM memory_vec WHERE rowid IN ({rowid_ph})",
                            tuple(rowids),
                        )
                    conn.execute(
                        f"DELETE FROM memory_unit WHERE unit_id IN ({placeholders})",
                        tuple(unit_ids),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_units(self, scope: Scope) -> list[tuple[str, bytes]]:
        """全量拉 ``(unit_id, unit_json bytes)``，供 list 接口内存过滤排序分页（F07 §5.3）。

        与 :meth:`get_units` 同口径：``scope`` 不参与过滤——影子索引靠 project+category
        隔离（§5 已定，不走 Scope 字段），scope 仅作签名占位对齐契约。**过滤/排序/分页
        不在此做**，交上层 ``CompositeStorage.list`` 文档分流后复用
        :func:`kv_impl.memory_list.list_memory_entries`（与 KV ``scan→list_memory_entries``
        同构：拉全量 raw → 内存过滤 memory_types/filters → 按 t_ingest 稳定排序 → 分页）。

        返回 ``list[tuple[str, bytes]]``，与 ``KVMemoryListResult.entries`` 同构——
        上层无需区分来源（KV scan 还是 shadow list_units），统一喂进
        ``list_memory_entries`` 即可。

        排序：此处仅按 ``unit_id`` 升序返回（稳定但不语义化），**最终排序由上层
        ``list_memory_entries._sort_key`` 按 ``t_ingest`` 稳定排序覆盖**（见该模块）——
        与 KV ``scan`` 不保证顺序、由上层排序的范式一致。此处不强加语义排序，避免
        与上层二次排序冲突、也避免大表场景下 SQL ORDER BY 的额外排序开销。

        ``unit_json`` 非法行（loads 归 None）不在此过滤——上层 ``list_memory_entries``
        已对 ``loads(raw) is None`` 跳过（见该模块 ``for key, raw in entries`` 循环），
        与 KV 路径容错一致。
        """
        with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT unit_id, unit_json FROM memory_unit ORDER BY unit_id"
            ).fetchall()
        return [(uid, bytes(raw)) for uid, raw in rows]

    def list_units_by_md(self, scope: Scope, md_filename: str) -> list[tuple[str, str]]:
        """按 ``md_filename`` 查该文件所有 unit，供看门狗同步用（F07 §12.3）。

        返回 ``(unit_id, content_hash)`` 二元组。看门狗比 md 文件现存 unit 与 shadow
        索引登记的 content_hash 判定漂移（F07 §12.3），故只需 ``content_hash`` 不需
        ``unit_json`` 全量。``scope`` 不参与过滤（同 list_units 口径）。

        无 ``md_filename`` 参数校验——空串/None 由 SQL 自然返空（``md_filename`` 列
        写入侧由 ``_md_filename_of`` 兜底，不会存空串，故空串查询必返空，无需特判）。
        """
        with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT unit_id, content_hash FROM memory_unit WHERE md_filename = ?",
                (md_filename,),
            ).fetchall()
        return [(uid, ch) for uid, ch in rows]

    # -- 召回（F07 §4.4 分两批，F08 §5 category 映射 memory_class）--------- #

    def search_fulltext(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        """FTS5 倒排召回，BM25 排序。按 F07 §4.4 分两批下推 project+category 过滤：

        - 批 1（受 project 隔离）：``project IN (...) AND category IN ('project_memory','team_memory')``
        - 批 2（全局可见）：``category='user_memory'``（无 project 限制）

        两批合并后按 score 取 top_k。query.text 先用注入 tokenizer 预分词成 token 串再 MATCH，
        与写入侧 insert_units 口径一致（F07 §11.5）。FTS5 是前置过滤，无召回不足风险（§4.4.2）。

        project 过滤值取自 ``query.filters`` 里的 ``system_metadata.project`` 谓词
        （上层 coords 折算下推，见 :meth:`_projects_from_filters`），无该谓词时落
        ``default``——等价不按 project 隔离，与团队记忆坐标缺项语义一致（F08 §1.2
        复用 coords，不走 Scope）。
        """
        with self._lock:
            conn = self._ensure_conn()
            tokens = self._tokenizer.tokenize(query.text)
            # OR 连接：FTS5 空格分隔是隐式 AND——查询分词中任一词不在文档里
            # （如「张三喜欢什么咖啡」里的「什么」）整条 0 命中。自然语言查询
            # 常带疑问词/停用词，AND 语义让召回普遍落空。改 OR 后任一词命中即
            # 进入候选，多词同命中的文档靠 bm25 名次靠前；代价是单高频词查询
            # 可能召回一批弱相关，由 top_k 截断与上层 RRF 名次合并消化。
            match_expr = " OR ".join(tokens)
            if not match_expr:
                return []
            projects = self._projects_from_filters(query.filters)
            # 系统前置谓词（lifecycle/t_valid/t_invalid/t_event）下推：对齐非文档流程
            # build_system_filters 的索引级排除（见 _compile_system_filters），点读后复核兜底。
            sys_sql, sys_params = _compile_system_filters(query.filters)
            sys_where = f" AND ({sys_sql})" if sys_sql else ""
            k = query.top_k

            # 批 1：受 project 隔离的类别
            proj_ph = ",".join("?" for _ in projects)
            cls_ph = ",".join("?" for _ in _PROJECT_SCOPED_CLASSES)
            rows1 = conn.execute(
                f"SELECT m.unit_id, bm25(memory_fts) AS score "
                f"FROM memory_fts JOIN memory_unit m ON m.rowid = memory_fts.rowid "
                f"WHERE memory_fts MATCH ? AND m.project IN ({proj_ph}) "
                f"AND m.category IN ({cls_ph}) "
                f"{sys_where} "
                f"ORDER BY score LIMIT ?",
                (match_expr, *projects, *_PROJECT_SCOPED_CLASSES, *sys_params, k),
            ).fetchall()
            # 批 2：全局可见类别（无 project 限制）
            rows2 = conn.execute(
                "SELECT m.unit_id, bm25(memory_fts) AS score "
                "FROM memory_fts JOIN memory_unit m ON m.rowid = memory_fts.rowid "
                "WHERE memory_fts MATCH ? AND m.category = ? "
                f"{sys_where} "
                "ORDER BY score LIMIT ?",
                (match_expr, _GLOBAL_CLASS, *sys_params, k),
            ).fetchall()
        # 两批合并（profile 与 project 隔离类别互不重叠，无去重需求，见 §4.4），按 score 降序取 top_k
        merged = list(rows1) + list(rows2)
        merged.sort(key=lambda r: r[1])  # bm25 越小越相关（负值）
        return [ScoredID(id=r[0], score=r[1]) for r in merged[:k]]

    def search_vector(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        """sqlite-vec KNN 向量召回。按 F07 §4.4 分两批下推 project+category 过滤。

        vec0 是 post-filter（§4.4.3）：先按距离取 top-k 近邻，再 JOIN WHERE 过滤。
        project 隔离度高时召回不足，批 1 用过采样兜底（``k * _oversample`` 取近邻，
        过滤后按 distance 排序截断到目标 k）。批 2 无 project 隔离，不启过采样。

        **降级模式**（``vec_enabled=False``）：返回空列表，不抛错——让上层检索编排照常运行，
        只是向量召回缺一路（倒排召回仍可用）。
        """
        if not self.vec_enabled:
            return []
        with self._lock:
            conn = self._ensure_conn()
            projects = self._projects_from_filters(query.filters)
            # 系统前置谓词下推（与 search_fulltext 同口径，见 _compile_system_filters）。
            sys_sql, sys_params = _compile_system_filters(query.filters)
            sys_where = f" AND ({sys_sql})" if sys_sql else ""
            k = query.top_k
            qblob = _vec_to_blob(query.vector)

            # 批 1：受 project 隔离 + 过采样兜底（post-filter 召回不足风险，§4.4.3）
            oversample = k * _DEFAULT_OVERSAMPLE
            proj_ph = ",".join("?" for _ in projects)
            cls_ph = ",".join("?" for _ in _PROJECT_SCOPED_CLASSES)
            rows1 = conn.execute(
                f"SELECT m.unit_id, v.distance AS score "
                f"FROM memory_vec v JOIN memory_unit m ON m.rowid = v.rowid "
                f"WHERE v.embedding MATCH ? AND k = ? "
                f"AND m.project IN ({proj_ph}) AND m.category IN ({cls_ph}) "
                f"{sys_where} "
                f"ORDER BY v.distance LIMIT ?",
                (qblob, oversample, *projects, *_PROJECT_SCOPED_CLASSES, *sys_params, k),
            ).fetchall()
            # 批 2：全局可见类别，无 project 过滤，不启过采样
            rows2 = conn.execute(
                "SELECT m.unit_id, v.distance AS score "
                "FROM memory_vec v JOIN memory_unit m ON m.rowid = v.rowid "
                "WHERE v.embedding MATCH ? AND k = ? "
                "AND m.category = ? "
                f"{sys_where} "
                "ORDER BY v.distance LIMIT ?",
                (qblob, k, _GLOBAL_CLASS, *sys_params, k),
            ).fetchall()
        merged = list(rows1) + list(rows2)
        # distance 越小越相关；升序取 top_k
        merged.sort(key=lambda r: r[1])
        # vec0 distance 是距离（越小越相关），转 ScoredID 时取负让"越大越相关"口径统一
        return [ScoredID(id=r[0], score=-r[1]) for r in merged[:k]]


def _vec_to_blob(vec: list[float]) -> bytes:
    """float 向量 → sqlite-vec 期望的小端 float32 连续字节。"""
    import struct

    return struct.pack(f"<{len(vec)}f", *vec)


# -- 注册到 ShadowIndexProducer（实现自注册，新增无需改 producer/build_kernel） -- #

logger = get_logger(__name__)


@ShadowIndexProducer.register("sqlite")
def _build(config):
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    # 跨 namespace 读 markdown_store.default 的 root，供 db_path 派生（F07 §6.6）。
    # markdown store 的 root 在它自己的 namespace params 里（不在 shadow 的 params），
    # 须经 ctx.lookup 跨 namespace 取。未配 markdown_store 时 root 为 None → db_path 回退兜底。
    markdown_root = None
    try:
        md_spec = config.ctx.lookup("markdown_store", "default")
        markdown_root = (md_spec.params or {}).get("root")
    except Exception as e:
        # 未配 markdown_store 是正常状态（走 db_path 兜底），但配置损坏也会走到这里，
        # 打 debug 日志让回退可观测，避免静默行为偏移无迹可查。
        logger.error("get markdown_store.default failed, shadow sqlite db_path use default path, exception msg: %s", e)
    # embedder 可选（降级）：config 显式配了 embedder 才注入，否则传 None → 降级模式。
    # 不用 dep(default=...) 是因为 default=None 会抛 ValidationError（dep 无 default 报错），
    # 这里要的是"未配就 None"而非"未配就报错"。
    embedder = None
    if "embedder" in config.params:
        embedder = EmbedderProducer.dep(config)
    return SqliteDocumentShadowIndex(
        Factory.cfg_get(config, "db_path", None),
        embedder=embedder,
        tokenizer=TokenizerProducer.dep(config, default="jieba"),
        config_source=ConfigSourceProducer.get_cached("default"),
        markdown_root=markdown_root,
    )
