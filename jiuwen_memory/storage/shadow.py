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
    def get_units(self, scope: Scope, unit_ids: list[str]) -> list[MemoryUnit]:
        """按 ``unit_id`` 点查全量 ``MemoryUnit``（从 ``unit_json`` 列反序列化）。缺失 id 省略。"""

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
    def list_units_by_md(self, scope: Scope, md_filename: str) -> list[tuple[str, str]]:
        """按 ``md_filename`` 查该文件所有 unit，供看门狗同步用。返回 ``(unit_id, content_hash)`` 二元组。"""

    @abstractmethod
    def search_fulltext(self, scope: Scope, query: TextQuery) -> list[ScoredID]:
        """FTS5 倒排检索，BM25 排序，返回 top-k ``(unit_id, score)``。project+category 过滤
        在算子内部分两批下推（F08 §5：category 落 memory_class 值）。
        """

    @abstractmethod
    def search_vector(self, scope: Scope, query: VectorQuery) -> list[ScoredID]:
        """sqlite-vec KNN 检索，返回 top-k ``(unit_id, score)``。project+category 过滤
        在算子内部分两批下推（post-filter，批 1 需过采样兜底）。
        """

    def store_type(self) -> StoreType:
        return StoreType.DOCUMENT_SHADOW
