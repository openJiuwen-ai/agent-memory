"""DocumentShadowIndex — 文档场景影子索引（sqlite3 + sqlite-vec 复合算子）。

承载全量 ``MemoryUnit`` 存储 + 按 ``unit_id`` 点查 + fulltext 倒排 + 向量检索，
四者同库（同一 sqlite 文件、同一连接、靠 ``unit_id``/隐式 ``rowid`` 关联）。
文档场景下（``write_document=true``）替代 KV 成为真源：``add`` 不写 KV，
写 md + 调 ``insert_units`` 建影子索引（见 F07 §3.1 / F08 §4 步骤6）。

与现有 ``FulltextStore``/``VectorStore``/``KVStore`` 单一契约不同，本算子是**复合算子**：
写入入口是全量 ``MemoryUnit``（非 Document/VectorRecord 投影），投影中的 ``content``
正文、``embedding`` 向量在算子内部派生；唯 ``md_filename`` 例外——它是 ``md.write``
落盘后的产物，由 ``md.write`` 回填进 ``unit.system_metadata[MD_FILENAME_KEY]`` 后传入，
算子从 system_metadata 读取落库，不在算子内部派生（F07 §11.3）。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, Scope

from .base import BaseStore, StoreType
from .types import ScoredID, TextQuery, VectorQuery


class ShadowIndexProducer(Factory):
    """DocumentShadowIndex 的注册式工厂（与契约同处接口层）。

    ``name`` 即后端名（如 sqlite）。各实现在 ``shadow_impl`` 下以
    ``@ShadowIndexProducer.register("<后端>")`` 自注册——注册发生在 import 实现模块时，
    由 :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "shadow_index"


class DocumentShadowIndex(BaseStore):
    """文档场景影子索引契约。"""

    @abstractmethod
    def insert_units(self, scope: Scope, units: list[MemoryUnit]) -> None:
        """存全量 ``MemoryUnit``（``memory_codec.dumps`` 序列化为 ``unit_json``），
        同步写 ``content_hash`` + ``md_filename`` + FTS5 倒排 + vec0 向量投影。
        """

    @abstractmethod
    def get_units(self, scope: Scope, unit_ids: list[str], *, relax_session: bool = False) -> list[MemoryUnit]:
        """按 ``unit_id`` 点查全量 ``MemoryUnit``（从 ``unit_json`` 列反序列化）。缺失 id 省略。

        ``relax_session=True`` 放 session 段等值（仅检索物化侧），与
        ``search_fulltext``/``search_vector`` 的召回放宽口径对齐，加载跨 session 命中的 unit。
        """

    @abstractmethod
    def find_unit_ids_by_content_hash(
        self, scope: Scope, content_hash: str
    ) -> list[str]:
        """按 ``content_hash`` 在 scope 内反查 ``unit_id`` 列表（F08 §D3）。

        ``read_md_file`` / ``edit_md_file`` 的 hash 反查落点：读 md 按块算
        ``content_hash``（与 ``_content_hash`` 同口径 ``sha256`` 不归一化）后，用本方法
        反查命中的 ``unit_id``，再经 :meth:`get_units` 取完整 ``MemoryUnit``。

        scope 5 段等值隔离（与 :meth:`get_units` 同口径，**非** :meth:`list_units_by_md`
        的跨 scope 例外口径）——read/edit 是用户态接口须严格按 scope 隔离，看门狗诊断
        才跨 scope。走已有 ``idx_content_hash`` 索引（O(log n) 点查）。

        返回值未保证顺序（SQLite 默认 rowid 升序）；同 ``content_hash`` 在同 scope 下
        可命中多个 unit（不同 md 文件写了相同正文行），返回全部命中。空命中返回空列表。
        """

    @abstractmethod
    def update_units(self, scope: Scope, units: list[MemoryUnit]) -> None:
        """覆写全量 ``unit_json``。id 不存在报缺失。

        投影重建按 ``content_hash`` 变化判定：content_hash 变（content 改）→ 重建 FTS5 + vec0；
        content_hash 未变（只改状态字段）→ 只覆写 ``unit_json``，不重建投影。
        """

    @abstractmethod
    def delete_units(self, scope: Scope, unit_ids: list[str]) -> None:
        """按 ``unit_id`` 删全量 + 投影（幂等）。同事务显式删三表（external content 不级联）。"""

    @abstractmethod
    def list_units(self, scope: Scope) -> list[tuple[str, bytes]]:
        """按 scope 全量拉 ``(unit_id, unit_json bytes)``，供 list 接口内存过滤排序分页。"""

    @abstractmethod
    def list_units_by_md(
        self, scope: Scope, md_filename: str, *, lifecycle: str | None = None
    ) -> list[tuple[str, str]]:
        """按 ``md_filename`` 查该文件所有 unit，供看门狗同步用。

        返回 ``(unit_id, content_hash)`` 二元组。``lifecycle`` 可选过滤：传
        ``LifecycleState.ACTIVE.value`` 只取 active 行（看门狗对账只认活跃行——非
        active 行是 SUPERSEDE/治理留下的历史版本，本就不该在 md 里、不参与 hash
        对账）；None 不过滤（保持跨 scope 诊断原行为）。
        """

    @abstractmethod
    def latest_scope_by_md(self, scope: Scope, md_filename: str) -> Scope | None:
        """按 ``md_filename`` 查该文件最新一条 unit 的 scope，无历史返 None。

        供看门狗建新 unit 时继承 scope（md 文件不编码 scope，按同文件最新归属近似）。
        不限 scope WHERE（对齐 ``list_units_by_md`` 的看门狗跨 scope 诊断例外）。
        排序口径 rowid DESC（最近插入）；无历史返 None。
        """

    @abstractmethod
    def search_fulltext(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        """FTS5 倒排检索，BM25 排序，返回 top-k ``(unit_id, score)``。

        单批按 ``project`` 过滤下推（路径甲：召回只看 project 过滤条件，不再按
        ``category``/``memory_class`` 分批）——``project IN (...)`` 取自 ``query.filters``
        里的 ``system_metadata.project`` 谓词，命中空串行（跨项目可见）+ 本项目行。
        ``category`` 维度不在召回 SQL 过滤；若需按类别收窄，上层应通过 ``query.filters``
        显式传 category 谓词（F08 §5 路径甲）。
        """

    @abstractmethod
    def search_vector(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        """sqlite-vec KNN 检索，返回 top-k ``(unit_id, score)``。

        单批按 ``project`` 过滤下推（路径甲：与 :meth:`search_fulltext` 同口径，只看
        project 过滤条件，不再按 category 分批）。vec0 是 post-filter，project 隔离度
        高时召回不足由实现侧过采样兜底。

        降级协议：``vec_enabled`` 为 False 时返回空列表且不抛错——调用方应先查
        ``vec_enabled`` 区分「能力不可用」与「零召回」，而非仅凭空结果推断。
        """

    @property
    def vec_enabled(self) -> bool:
        """向量召回是否可用（完整模式，建了 ``memory_vec`` 表）。

        False 表示降级模式（embedder 未注入 / sqlite_vec 不可导入或 load 失败）——
        ``search_vector`` 返回空列表且不抛错。调用方应先查 ``vec_enabled`` 区分
        「能力不可用」与「零召回」。缺省 False（安全关闭），完整模式实现覆盖为 True。
        """
        return False

    def scopes(self) -> list[Scope]:
        """枚举影子索引已有 Scope（对齐 :meth:`KVStore.scopes`）。

        供文档模式 lifecycle/space sweep 枚举 scope。缺省空列表（安全关闭）——
        完整实现覆盖为 ``SELECT DISTINCT`` 五段返回 list[Scope]；未装配文档端口时
        契约默认空，调用方不应据此推断「无 unit」（应经 ``has_shadow_index`` 判能力）。
        """
        return []

    def store_type(self) -> StoreType:
        return StoreType.DOCUMENT_SHADOW
