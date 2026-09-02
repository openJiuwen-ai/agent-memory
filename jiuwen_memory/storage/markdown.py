"""MarkdownStore — 人类可读视图存储：把 MemoryUnit 的正文写进 md 文件。

文档记忆（F08）下，md 是**人类可读视图**，不是反解真源——召回走影子索引按
``unit_id`` 取全量，不靠 md 反解。md 只承载 ``segments[0].content`` 正文 + 标题，
元数据不进 md。

``scope`` 为显式第一入参（与全系 Store 一致），但文档场景下 md 落盘路径由
``memory_class``（归属类别）+ ``project``（coords 坐标）决定（F08 §3 映射），
而非 scope 的 org/space/user 多级——md 文件按 project 归档、按 memory_class 分流到
USER.md / MEMORY.md / daily_memory/YYYY-MM-DD.md。

``write`` 的副作用约定（F08 §3.2 / F07 §3.2）：落盘后**就地回填**
``unit.system_metadata[MD_FILENAME_KEY]``（md 文件相对根目录的路径），供影子索引
``insert_units`` 从 system_metadata 读取后落 ``memory_unit.md_filename`` 列。回填是
对 unit 对象的就地修改，不另设返回值。
"""

from __future__ import annotations

from abc import abstractmethod

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import MemoryUnit, Scope

from .base import BaseStore


class MarkdownProducer(Factory):
    """MarkdownStore 的注册式工厂（与契约同处接口层，消费方只依赖接口即可取实例）。

    ``name`` 即后端名（如 local）。各实现在 ``markdown_impl`` 下以
    ``@MarkdownProducer.register("<后端>")`` 自注册——注册发生在 import 实现模块时，由
    :func:`storage.bootstrap.register_backends` 统一触发。
    """

    TOP_NAME = "markdown_store"


class MarkdownStore(BaseStore):
    """人类可读 md 视图存储。

    文档场景下与影子索引并列（真源是影子索引，md 是人类视图）。``write`` 落盘后
    回填 ``md_filename`` 进 unit.system_metadata，影子索引据此建立指向关系。
    """

    @abstractmethod
    def write(self, scope: Scope, units: list[MemoryUnit]) -> None:
        """把一批 unit 的 ``segments[0].content`` 正文写进按 F08 §3 映射算出的 md 文件。

        同一批 unit 可能落到**不同 md 文件**（不同 memory_class/project），实现内部按
        文件分组追加——同一文件（如按天聚合的 daily_memory、固定 MEMORY.md）的多条
        unit 按块追加进同一文件。整批共持一次锁（避免每条 unit 各拿一次锁的开销），
        对齐 :meth:`CompositeStorage.add` 的批量语义。

        副作用：每个 unit 落盘后**就地回填** ``unit.system_metadata[MD_FILENAME_KEY]``
        为该 md 文件相对根目录的路径（含文件名），供影子索引 ``insert_units`` 从
        system_metadata 读取后落 ``memory_unit.md_filename`` 列，也供看门狗据此定位
        文件变化。

        ``memory_class`` 为空时在此兜底赋 ``team_memory``（F08 §2），保证 md 路径与
        影子索引 ``category`` 列落值一致。
        """

    @abstractmethod
    def replace_content(
        self, scope: Scope, md_filename: str, old_content: str, new_content: str
    ) -> bool:
        """在 md 文件里定位含 ``old_content`` 的块，替换为 ``new_content``（F07 §5.2.3）。

        update 的 OVERWRITE 步骤 ⑤ 调用：影子索引 ``update_units`` 覆写 ``unit_json`` +
        重建投影（步骤 ③，§5.2.1）后，md 侧需同步把旧 content 块替换为新 content 块，
        让 md 与影子索引一致。

        块定位：md 块结构 ``<标题>\\n<正文>\\n\\n``（见 :meth:`_render_block`），按
        ``\\n\\n`` 切块，逐块比对正文 == ``old_content`` 命中——正文单行（§12.4 已
        确认 md 写入一个 unit 一行正文），切分口径与看门狗按行遍历一致（§12.3）。

        返回是否命中替换（未命中返回 False，调用方据决定是否告警——md 与索引漂移
        属异常，正常流程步骤 ③ 已改 content，md 必命中）。

        **并发**：复用 ``write`` 同款锁（进程内串行化），路径安全校验复用
        :meth:`_resolved_root` + ``os.path.join``（与 ``write`` 同口径）。
        """

    @abstractmethod
    def remove_content(self, scope: Scope, md_filename: str, content: str) -> bool:
        """在 md 文件里定位含 ``content`` 的块，删除该块（F07 §5.4）。

        delete 调用：影子索引 ``delete_units`` 删三表后，md 侧需同步删掉对应块，
        让 md 与影子索引一致。

        块定位：与 :meth:`replace_content` 同口径——按 ``\\n\\n`` 切块，逐块比对正文
        == ``content`` 命中，删除**首个**命中块（保留其余块与块间 ``\\n\\n`` 分隔），
        返回是否命中删除。未命中（md 与索引漂移，如手改 md / 已被看门狗删除）返回
        False，调用方据决定是否告警。

        **并发**：复用 ``write`` 同款锁（进程内串行化），路径安全校验复用
        :meth:`_resolved_root` + ``os.path.join``（与 ``write`` 同口径）。
        """
