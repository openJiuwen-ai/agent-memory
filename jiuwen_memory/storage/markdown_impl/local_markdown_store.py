"""落盘实现：:class:`~storage.markdown.MarkdownStore` 的本地文件后端。

md 文件是文档记忆（F08）的人类可读视图——只记 ``segments[0].content`` 正文 + 标题，
不记元数据。落盘路径按 ``memory_class``（归属类别）+ ``project``（coords 坐标）映射
（F08 §3）：

    user_memory  → {root}/memory/USER.md                          （跨 project，memory 根下）
    project_memory → {root}/memory/{project|default}/MEMORY.md    （单文件，块追加）
    team_memory  → {root}/memory/{project|default}/daily_memory/YYYY-MM-DD.md  （按天聚合）
    空（兜底）  → 同 team_memory（F08 §2）

``write`` 落盘后就地回填 ``unit.system_metadata[MD_FILENAME_KEY]``（相对根路径），
供影子索引 ``insert_units`` 从 system_metadata 读取后落 ``memory_unit.md_filename`` 列。

并发：首版用 ``threading.Lock`` 串行化进程内文件访问（对齐 SQLiteKVStore 范式）。
跨进程文件锁待后续加（本地单进程场景够用）。
"""

from __future__ import annotations

import datetime
import os
import threading

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import (
    COORDS_KEY,
    MD_FILENAME_KEY,
    MD_TITLE_KEY,
    MEMORY_CLASS_KEY,
    MemoryUnit,
    Scope,
)
from jiuwen_memory.config.binding import resolve_connection_url
from jiuwen_memory.storage.base import StoreType
from jiuwen_memory.storage.markdown import MarkdownProducer, MarkdownStore

# memory_class → (子路径模板, 是否进 project 子目录)
# user_memory 不进 project 子目录（跨项目用户画像）；project/team 进 project 子目录。
# 详见 F08 §3 映射表。
_PATH_MAP: dict[str, tuple[str, bool]] = {
    "user_memory": ("USER.md", False),
    "project_memory": ("MEMORY.md", True),
    "team_memory": ("daily_memory", True),  # daily_memory/ 下再拼 YYYY-MM-DD.md
}

_DEFAULT_CLASS = "team_memory"
_DEFAULT_PROJECT = "default"


class LocalMarkdownStore(MarkdownStore):
    """本地文件 md 视图存储：按 memory_class + project 分流落盘。

    ``root`` 可经 ConfigSource ``markdown_store.root`` 晚绑定；路径变化时下次写重建。
    构造期确保根目录存在。
    """

    def __init__(
        self,
        root: str = "",
        *,
        config_source=None,
        config_namespace: str = "markdown_store",
    ) -> None:
        self._fallback_root = root
        self._config_source = config_source
        self._config_namespace = config_namespace
        self._lock = threading.Lock()

    def store_type(self) -> StoreType:
        return StoreType.MARKDOWN

    def health(self) -> None:
        root = self._resolved_root()
        if not root or not os.path.isdir(root):
            from jiuwen_memory.common.errors import HealthCheckError

            raise HealthCheckError(f"markdown root not a directory: {root!r}")
        return None

    # -- MarkdownStore 契约 -------------------------------------------------- #

    def write(self, scope: Scope, units: list[MemoryUnit]) -> None:
        with self._lock:
            root = self._resolved_root()
            self._ensure_dir(root)
            # 按 md 文件分组：同文件的多条 unit 的块拼一起一次追加写（减少 IO）
            # 同时回填每个 unit 的 md_filename + 兜底后的 memory_class
            groups: dict[str, list[tuple[MemoryUnit, str]]] = {}
            for unit in units:
                metadata = dict(unit.system_metadata or {})
                # 先算兜底后的 memory_class（写回 + 供 _md_path 用，保证口径一致）
                memory_class = self._resolved_memory_class(metadata)
                metadata[MEMORY_CLASS_KEY] = memory_class
                # 临时塞回，让 _md_path 读到兜底后的值
                unit.system_metadata = metadata
                md_filename = self._md_path(unit)
                metadata[MD_FILENAME_KEY] = md_filename
                unit.system_metadata = metadata  # 回填最终值
                content = self._unit_content(unit)
                block = self._render_block(unit, content, memory_class)
                groups.setdefault(md_filename, []).append((unit, block))

            # 每个文件一次追加写：把同文件所有块拼一起写
            for md_filename, pairs in groups.items():
                abs_path = os.path.join(root, md_filename)
                self._ensure_dir(os.path.dirname(abs_path))
                merged = "".join(block for _, block in pairs)
                with open(abs_path, "a", encoding="utf-8") as fh:
                    fh.write(merged)

    def replace_content(
        self, scope: Scope, md_filename: str, old_content: str, new_content: str
    ) -> bool:
        """在 md 文件定位含 ``old_content`` 的块，替换为 ``new_content``（F07 §5.2.3）。

        块结构 ``<标题>\\n<正文>\\n\\n``（见 :meth:`_render_block`）。按 ``\\n\\n`` 切块，
        逐块比对「块首行（标题）后的正文」== ``old_content`` 命中——正文单行（§12.4），
        故块 = ``# {id}\\n{content}``（尾部 ``\\n\\n`` 是块间分隔，切块后余下），正文 = 块
        去首行（标题）+ 去前导换行后的剩余单行。

        命中后**整块替换**为 ``# {原 id}\\n{new_content}\\n\\n``：保留原标题行（unit_id 不变，
        OVERWRITE 同 id 原地改写，§5.2.1），只换正文。块间分隔的 ``\\n\\n`` 由拼接复原。

        未命中（``old_content`` 不在文件任何块）返回 False——正常流程步骤 ③ 已改 content，
        md 必命中；未命中说明 md 与索引已漂移（如手改 md），调用方据决定告警/触发看门狗。

        并发：复用 ``write`` 同款锁（进程内串行化）；路径校验同 ``write``（root + 相对路径
        join，``md_filename`` 不允许含 ``..`` 越权——由 ``_resolved_root`` + ``join`` 保障）。
        """
        with self._lock:
            root = self._resolved_root()
            abs_path = os.path.join(root, md_filename)
            if not os.path.isfile(abs_path):
                return False
            with open(abs_path, "r", encoding="utf-8") as fh:
                text = fh.read()

            # 块序列：按 \n\n 切（与 _render_block 尾部 \n\n 对齐，块间以此为界）。
            # 末尾 \n\n 会产生末尾空串，filter 掉。
            raw_blocks = text.split("\n\n")
            blocks = [b for b in raw_blocks if b]

            replaced = False
            new_blocks: list[str] = []
            for block in blocks:
                # 块结构：首行标题（# {id}）+ 第二行正文。取标题后的正文比对。
                # split("\n", 1)：[0]=标题，[1]=正文（若无换行，说明块格式异常，跳过）。
                parts = block.split("\n", 1)
                if len(parts) != 2:
                    new_blocks.append(block)
                    continue
                title, body = parts[0], parts[1]
                if body == old_content and not replaced:
                    # 命中：整块替换为「原标题 + 新正文」，保留 unit_id（OVERWRITE 同 id）。
                    # 替换首个命中（old_content 重复时只改第一处，§5.2.3 注：重复 content
                    # 字符串匹配会误替多块——首版取首个，后续可加 unit_id 重载优化锚点）。
                    new_blocks.append(f"{title}\n{new_content}")
                    replaced = True
                else:
                    new_blocks.append(block)

            if not replaced:
                return False

            # 还原：块间用 \n\n 拼接，末尾补 \n\n（与 _render_block 尾部 \n\n 对齐，
            # 保证后续 write 追加 / 看门狗按行遍历口径不变）。
            out = "".join(f"{b}\n\n" for b in new_blocks)
            with open(abs_path, "w", encoding="utf-8") as fh:
                fh.write(out)
            return True

    def remove_content(self, scope: Scope, md_filename: str, content: str) -> bool:
        """在 md 文件里定位含 ``content`` 的块，删除该块（F07 §5.4）。

        与 :meth:`replace_content` 同口径切块/比对（``\\n\\n`` 切块，标题行后正文
        == ``content`` 命中），差别只在命中后动作——**删除该块**（不加入新块序列），
        其余块原样保留。删除首个命中（content 重复时只删第一处，与 replace_content
        「重复 content 字符串匹配会误替多块」同款限制，见 §5.2.3）。

        未命中（``content`` 不在文件任何块）返回 False——delete 正常流程影子索引已删该
        unit，md 必命中；未命中说明 md 与索引已漂移（如手改 md / 看门狗先删），调用方
        据决定告警/触发看门狗（§12.3）。

        并发：复用 ``write``/``replace_content`` 同款锁（进程内串行化）；路径校验同
        ``write``（root + 相对路径 join，``md_filename`` 不允许含 ``..`` 越权）。
        """
        with self._lock:
            root = self._resolved_root()
            abs_path = os.path.join(root, md_filename)
            if not os.path.isfile(abs_path):
                return False
            with open(abs_path, "r", encoding="utf-8") as fh:
                text = fh.read()

            # 块序列：按 \n\n 切（与 _render_block 尾部 \n\n 对齐，块间以此为界）。
            # 末尾 \n\n 会产生末尾空串，filter 掉。
            raw_blocks = text.split("\n\n")
            blocks = [b for b in raw_blocks if b]

            removed = False
            new_blocks: list[str] = []
            for block in blocks:
                # 块结构：首行标题（# {id}）+ 第二行正文。取标题后的正文比对。
                # split("\n", 1)：[0]=标题，[1]=正文（若无换行，说明块格式异常，跳过）。
                parts = block.split("\n", 1)
                if len(parts) != 2:
                    new_blocks.append(block)
                    continue
                title, body = parts[0], parts[1]
                if body == content and not removed:
                    # 命中：删除该块（不加入新块序列）。删除首个命中，与 replace_content
                    # 首版「取首个」同款语义（content 重复时只删第一处）。
                    removed = True
                else:
                    new_blocks.append(block)

            if not removed:
                return False

            # 还原：剩余块间用 \n\n 拼接，末尾补 \n\n（与 replace_content 口径一致，
            # 保证后续 write 追加 / 看门狗按行遍历口径不变）。删空后 out 为空串，
            # 写回空文件（全部块已删，与影子索引全删对齐）。
            out = "".join(f"{b}\n\n" for b in new_blocks)
            with open(abs_path, "w", encoding="utf-8") as fh:
                fh.write(out)
            return True

    # -- 路径计算 ------------------------------------------------------------ #

    def _md_path(self, unit: MemoryUnit) -> str:
        """按 F08 §3 映射算 md 文件相对根目录的路径。

        读 memory_class（空兜底 team_memory）+ coords.project（空兜底 default）。
        """
        metadata = unit.system_metadata or {}
        memory_class = self._resolved_memory_class(metadata)
        project = self._project_of(unit)

        # 未知 memory_class 走 team_memory 兜底（F08 §3.1 首版策略）
        sub, into_project = _PATH_MAP.get(memory_class, _PATH_MAP[_DEFAULT_CLASS])

        if not into_project:
            # user_memory：跨 project 放 memory 根下
            return f"memory/{sub}"

        if sub == "daily_memory":
            date = datetime.date.today().isoformat()
            return f"memory/{project}/daily_memory/{date}.md"
        # project_memory
        return f"memory/{project}/{sub}"

    @staticmethod
    def _resolved_memory_class(metadata: dict) -> str:
        """读 memory_class，空兜底 team_memory（F08 §2）。

        write 与 _md_path 共用此方法，保证 md 路径算值与回填进 system_metadata
        的值（进而影子索引 category 列）口径一致。
        """
        return str(metadata.get(MEMORY_CLASS_KEY) or "").strip() or _DEFAULT_CLASS

    @staticmethod
    def _project_of(unit: MemoryUnit) -> str:
        """从 coords 取 project，空落 default。coords 是 dict[str, str]。"""
        metadata = unit.system_metadata or {}
        coords = metadata.get(COORDS_KEY)
        if isinstance(coords, dict):
            project = str(coords.get("project") or "").strip()
            if project:
                return project
        return _DEFAULT_PROJECT

    # -- 渲染 ---------------------------------------------------------------- #

    @staticmethod
    def _unit_content(unit: MemoryUnit) -> str:
        """取 segments[0].content（文档模式一 unit 一 content，F08 §3.4）。"""
        if unit.segments:
            return unit.segments[0].content
        return ""

    @staticmethod
    def _render_block(unit: MemoryUnit, content: str, memory_class: str) -> str:
        """渲染一个 md 块：标题行 + 正文行 + 空行分隔。

        标题分流（F08 §8.2）：

        - daily 文件（team_memory / 兜底类，落 ``daily_memory/日期.md``）：
          标题 = ``coords["team"]``——daily 按天聚合多人多来源的记忆，标题行用 team 名
          标识本条记忆的来源团队；team 坐标缺失兜底 ``unit.id``。
        - 其余文件（USER.md / MEMORY.md）：标题 = ``system_metadata["md_title"]``
          （LLM 抽取时与 tier/tags 同 prompt 生成，见 extractor）；缺失兜底 ``unit.id``
          （infer=false 直写与看门狗重建路径无 LLM 标题，维持占位现状）。

        正文是单行（看门狗按行切分前提，F07 §12.4）。块间靠尾部空行分隔。
        标题行在任何读回路径中不被解析（看门狗跳过 ``#`` 行、replace/remove 按正文
        定位块、影子索引不存标题），标题内容变化对机器路径零影响。
        """
        if memory_class == "team_memory":
            metadata = unit.system_metadata or {}
            coords = metadata.get(COORDS_KEY)
            team = ""
            if isinstance(coords, dict):
                team = str(coords.get("team") or "").strip()
            title = team or unit.id
        else:
            title = str((unit.system_metadata or {}).get(MD_TITLE_KEY) or "").strip() or unit.id
        return f"# {title}\n{content}\n\n"

    # -- 内部 ---------------------------------------------------------------- #

    def _resolved_root(self) -> str:
        """从 ConfigSource 晚绑定读 markdown_store.root；缺失回落构造期默认值。"""
        live = resolve_connection_url(
            self._config_source,
            namespace=self._config_namespace,
            field="root",
            fallback=self._fallback_root or None,
        )
        return live or self._fallback_root

    @staticmethod
    def _ensure_dir(path: str) -> None:
        if path and not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)


# -- 注册到 MarkdownProducer（实现自注册，新增无需改 producer/build_kernel） -------- #


@MarkdownProducer.register("local")
def _build(config):
    from jiuwen_memory.config.config_source import ConfigSourceProducer

    return LocalMarkdownStore(
        Factory.cfg_get(config, "root", ""),
        config_source=ConfigSourceProducer.get_cached("default"),
    )
