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

    @property
    @abstractmethod
    def root(self) -> str:
        """解析后的 markdown 根目录（md 文件落盘与看门狗监听的基路径）。

        从 ConfigSource 晚绑定读 ``markdown_store.root``，缺失回退构造期默认值。
        供影子索引（派生 ``.shadow/shadow.db`` 路径）与看门狗（监听 ``{root}/memory/``）
        从**实例**读取，而非各自跨 namespace 摸 ``markdown_store`` 的 ``params.root``
        ——后者在 root 经 ConfigSource 晚绑定时读到的是过期的配置字面量。
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

        **并发**：复用 ``write`` 同款锁（进程内串行化），路径安全由实现的
        realpath 前缀校验收口（``md_filename`` 逃逸 ``..`` / 绝对路径抛错，与 ``write`` 同口径）。
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

        **并发**：复用 ``write`` 同款锁（进程内串行化），路径安全由实现的
        realpath 前缀校验收口（``md_filename`` 逃逸 ``..`` / 绝对路径抛错，与 ``write`` 同口径）。
        """

    @abstractmethod
    def read_content(
        self,
        scope: Scope,
        md_basename: str,
        *,
        project: str = "",
        line_range: tuple[int, int] | None = None,
    ) -> str:
        """读 md 文件原文（按 basename + project 解析路径），返回可选行切片后的全文。

        ``read_md_file`` 的存储层落点（F08 §D1）：``md_basename``（如 ``USER.md`` /
        ``MEMORY.md`` / ``2026-09-30.md``）+ ``project`` 经实现的 ``_resolve_md_path``
        映射为与 :meth:`_md_path`（按 memory_class 映射）**同口径**的相对路径，再经
        ``_safe_abs_path`` 收口为 root 内绝对路径读全文。

        ``line_range`` 为 ``(start, end)`` **1-based 闭区间**（``start=1, end=5`` 返回第
        1~5 行），``None`` 表示整文件，越界自动 clamp 到文件实际行数（超界返回空串）。
        切片按行（``splitlines(keepends=True)``）保行尾换行，切片结果拼回保持原文字节态。

        文件不存在返回空串（容错，不抛 ``NotFoundError``）——``read_md_file`` 上层按空串
        无正文块解析出空 unit list，与「文件无内容」语义一致；区分「不存在」与「空文件」
        由调用方先做判断。``md_basename`` 逃逸 ``..`` / 绝对路径由 ``_safe_abs_path`` 抛
        ``ValidationError``（与 ``write`` / ``replace_content`` 同口径）。
        """

    @abstractmethod
    def edit_content(
        self,
        scope: Scope,
        md_basename: str,
        old_text: str,
        new_text: str,
        *,
        project: str = "",
    ) -> None:
        """在 md 文件内对 ``old_text`` 做唯一替换为 ``new_text``（F08 §D5 / R5）。

        ``edit_md_file`` 的存储层落点：basename + project 解析路径后，读全文 →
        ``str.count(old_text)`` 校验唯一性 → ``replace(old_text, new_text, 1)`` 精确
        替换一处 → 整文件原子写。语义对齐 agent-core ``EditMemoryTool``：

        - ``old_text`` 不存在（count == 0）抛 ``NotFoundError``；
        - ``old_text`` 不唯一（count > 1）抛 ``ValidationError("not unique")``——
          与 :meth:`replace_content` 「重复 content 只改第一处」不同，edit 要求调用方
          提供唯一锚点（agent-core edit_memory 契约），首版取 fail-closed；
        - 文件不存在抛 ``NotFoundError``（edit 须有可改之物）；
        - 原子写：``open("w")`` 截断后 ``write`` 抛错时经 ``_safe_restore`` 回写读出的
          旧全文，与 :meth:`replace_content` / :meth:`remove_content` 同款补偿，让
          「异常等价于未发生」对 md 侧成立。

        ``old_text`` 任意文本（块内片段 / 跨多块均可），不强制对齐块边界——文件级
        子串替换，块结构重组（合并/拆分）由上层 ``_supersede_shadow_for_md`` 同步 shadow
        时按块解析处理。md 侧只保证文件级文本替换的原子性与唯一性。
        """

    @staticmethod
    def _resolve_md_path(md_basename: str, project: str) -> str:
        """basename + project → md 文件相对根路径（F08 §3 标准映射）。

        ``read_content`` / ``edit_content`` 内部用本方法把 basename + project 映射为
        相对路径（再经 ``_safe_abs_path`` 收口）；:class:`~storage.domain_store.DomainStore`
        经本 ABC 契约调用拿 ``rel_path``（供 ``shadow_index.list_units_by_md`` 等），
        不耦合具体实现（``LocalMarkdownStore``）。与实现的 ``_md_path``（按 memory_class
        映射）产出同一路径，但入参是 basename + project（不依赖完整 MemoryUnit）：

        - ``USER.md`` → ``memory/USER.md``（跨 project 用户画像，不进 project 子目录）；
        - ``MEMORY.md`` → project 非空 ``memory/{project}/MEMORY.md``，空
          ``memory/MEMORY.md``（跨项目可见）；
        - 其余（daily 文件名，如 ``2026-09-30.md``）→ project 非空
          ``memory/{project}/daily_memory/{basename}``，空 ``memory/daily_memory/{basename}``。

        不做路径安全校验——逃逸 ``..`` / 绝对路径由调用方 ``_safe_abs_path`` 收口
        抛 ``ValidationError``。
        """
        if md_basename == "USER.md":
            return "memory/USER.md"
        if md_basename == "MEMORY.md":
            return f"memory/{project}/MEMORY.md" if project else "memory/MEMORY.md"
        return (
            f"memory/{project}/daily_memory/{md_basename}"
            if project
            else f"memory/daily_memory/{md_basename}"
        )
