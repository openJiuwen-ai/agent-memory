"""落盘实现：:class:`~storage.markdown.MarkdownStore` 的本地文件后端。

md 文件是文档记忆（F08）的人类可读视图——只记 ``segments[0].content`` 正文 + 标题，
不记元数据。落盘路径按 ``memory_class``（归属类别）+ ``project``（coords 坐标）映射
（F08 §3）：

    user_profile → {root}/USER.md                                （跨 project，root 顶层）
    long_term_memory → {root}/memory/{project}/MEMORY.md         （单文件，块追加）
    daily_memory → {root}/memory/{project}/daily_memory/YYYY-MM-DD.md
    project 为空 → 落 memory 根下（memory/MEMORY.md / memory/daily_memory/...，跨项目可见）
    空 memory_class（兜底）→ 同 long_term_memory（MEMORY.md）

``write`` 落盘后就地回填 ``unit.system_metadata[MD_FILENAME_KEY]``（相对根路径），
供影子索引 ``insert_units`` 从 system_metadata 读取后落 ``memory_unit.md_filename`` 列。

并发：首版用 ``threading.Lock`` 串行化进程内文件访问（对齐 SQLiteKVStore 范式）。
跨进程文件锁待后续加（本地单进程场景够用）。
"""

from __future__ import annotations

import datetime
import os
import threading

from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
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
# user_profile 不进 project 子目录（跨项目用户画像，落 root 顶层）；long_term/daily 进 project 子目录。
# 详见 F08 §3 映射表。
# memory_class 取值域（F09）：user_profile / long_term_memory / daily_memory——agent_llm
# 抽取产出的归属类别，也是文档模式 md 落盘分流键。看门狗路径反推（_class_from_md_path）
# 按 basename/子目录反查，与本表对称。
_PATH_MAP: dict[str, tuple[str, bool]] = {
    "user_profile": ("USER.md", False),
    "long_term_memory": ("MEMORY.md", True),
    "daily_memory": ("daily_memory", True),
}

_DEFAULT_CLASS = "long_term_memory"  # 空/未知 memory_class 兜底 long_term_memory（MEMORY.md）
_DEFAULT_PROJECT = ""  # 无 project 时兜底空串：long_term/daily_memory 落 memory 根下（跨项目可见）

logger = get_logger(__name__)


def _safe_restore(abs_path: str, original_text: str) -> None:
    """写失败后回写旧全文，恢复 md 文件到调用前状态（S09 第 12 条原子写补偿）。

    ``replace_content`` / ``remove_content`` 的 ``open("w")`` 截断重写中，``write``
    抛错时文件已被截断——旧内容丢失，上层补偿（回滚 shadow）救不回 md。本函数在
    except 块内回写读出的旧全文 ``original_text``，让 md 恢复调用前字节态，使
    「异常等价于未发生」对 md 侧也成立。

    回写本身失败（IO 彻底损坏）只记 warning 不抛——避免掩盖原始写入异常；此时 md
    已残缺，漂移交看门狗对账（与补偿失败同款遗留）。回写用 ``open("w")`` 覆盖当前
    （已截断/残缺的）内容，``flush`` 确保落盘。
    """
    try:
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(original_text)
            fh.flush()
    except Exception as restore_exc:
        logger.warning(
            "md 原子写回滚失败 path=%s: %s（md 已残缺，漂移交看门狗对账）",
            abs_path, restore_exc,
        )


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

    @property
    def root(self) -> str:
        """解析后的 markdown 根目录（契约 ``MarkdownStore.root``）。"""
        return self._resolved_root()

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
                # 路径安全收口：md_filename 由 _md_path 拼出，但 project 坐标来自
                # 调用方可写的 coords——逃逸输入（../、绝对路径）在此抛 ValidationError。
                abs_path = self._safe_abs_path(root, md_filename)
                self._ensure_dir(os.path.dirname(abs_path))
                merged = "".join(block for _, block in pairs)
                with open(abs_path, "a", encoding="utf-8") as fh:
                    fh.write(merged)

    def restore_blocks(self, scope: Scope, units: list[MemoryUnit]) -> None:
        """补偿专用：按各 unit 的 ``md_filename``（system_metadata 保留键）直接追加块。

        与 ``write`` 的差别：``write`` 用 ``_md_path`` 重算路径（读 system_metadata.project
        或 coords.project），但补偿回写的 unit 是从影子索引 ``get_units`` 读回的——``dumps``
        剥除 coords（TRANSIENT 键），读回的 unit 无 coords；``system_metadata.project`` 非
        TRANSIENT 可留存，但仅 coords.project 的 unit（router/watchdog）读回仍落空 project →
        路径错位（原 ``memory/p1/MEMORY.md`` 错算成 ``memory/MEMORY.md``）。``md_filename`` 是
        ``write`` 时回填的相对路径（非 TRANSIENT，读回保留），直接用它定位文件绕过 project 取值。

        供 delete 多块补偿中途失败时回写已成功删除的块：循环 ``remove_content`` 第 N 块
        抛错时，前 N-1 块已真删，补偿 ``insert_units`` 回滚 shadow 后须把已删块追加回 md，
        才能让 md 与 shadow 同步回到调用前状态（F08「异常等价于未发生」）。

        块渲染复用 ``_render_block``（标题读 MD_TITLE_KEY / team，不依赖 coords）。
        ``md_filename`` 缺失的 unit 跳过（无法定位文件，漂移交看门狗对账）。
        """
        with self._lock:
            root = self._resolved_root()
            for unit in units:
                metadata = unit.system_metadata or {}
                md_filename = metadata.get(MD_FILENAME_KEY, "")
                if not md_filename:
                    continue
                memory_class = self._resolved_memory_class(dict(metadata))
                content = self._unit_content(unit)
                block = self._render_block(unit, content, memory_class)
                abs_path = self._safe_abs_path(root, md_filename)
                self._ensure_dir(os.path.dirname(abs_path))
                with open(abs_path, "a", encoding="utf-8") as fh:
                    fh.write(block)

    def replace_content(
        self, scope: Scope, md_filename: str, old_content: str, new_content: str
    ) -> bool:
        """在 md 文件定位含 ``old_content`` 的块，替换为 ``new_content``（F07 §5.2.3）。

        块结构 ``<标题>\\n<正文>\\n\\n``（见 :meth:`_render_block`）。按 ``\\n\\n`` 切块，
        逐块比对「块首行（标题）后的正文」== ``old_content`` 命中——正文单行（§12.4），
        故块 = ``# {标题}\\n{content}``（标题由 :meth:`_render_block` 生成：daily_memory
        用 coords.team，其余用 MD_TITLE_KEY；与 unit_id 无关；尾部 ``\\n\\n`` 是块间
        分隔，切块后余下），正文 = 块去首行（标题）+ 去前导换行后的剩余单行。

        命中后**整块替换**为 ``# {原标题}\\n{new_content}\\n\\n``：保留原块的首行（标题行），
        只换正文——OVERWRITE 同 unit_id 原地改写（§5.2.1），标题行内容不在替换范围。
        块间分隔的 ``\\n\\n`` 由拼接复原。

        未命中（``old_content`` 不在文件任何块）返回 False——正常流程步骤 ③ 已改 content，
        md 必命中；未命中说明 md 与索引已漂移（如手改 md），调用方据决定告警/触发看门狗。

        并发：复用 ``write`` 同款锁（进程内串行化）；路径安全走 ``_safe_abs_path``
        （realpath 前缀校验——``md_filename`` 源于调用方可写的 system_metadata，
        ``..`` / 绝对路径逃逸抛 ``ValidationError``，见该方法 docstring）。
        """
        with self._lock:
            root = self._resolved_root()
            abs_path = self._safe_abs_path(root, md_filename)
            if not os.path.isfile(abs_path):
                logger.debug("md %s not found, skip replace_content", md_filename)
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
                logger.warning(
                    "md block not found in %s (drift suspected)", md_filename
                )
                return False

            # 还原：块间用 \n\n 拼接，末尾补 \n\n（与 _render_block 尾部 \n\n 对齐，
            # 保证后续 write 追加 / 看门狗按行遍历口径不变）。
            out = "".join(f"{b}\n\n" for b in new_blocks)
            # 原子写（S09 第 12 条）：``open("w")`` 打开瞬间即截断文件，``write`` 中途
            # 抛错（磁盘满/IO）会让旧内容永久丢失——补偿路径（update 失败回滚 shadow）
            # 救不回 md，md/shadow 漂移、F08「异常等价于未发生」承诺落空。写失败时先
            # 回写读出的旧全文 ``text`` 恢复调用前状态，再重新抛原异常。``text`` 是上面
            # 读出的原始全文（含末尾 \\n\\n），回写后文件回到调用前字节态。
            try:
                with open(abs_path, "w", encoding="utf-8") as fh:
                    fh.write(out)
            except Exception:
                logger.warning(
                    "md atomic write failed %s, restored from snapshot",
                    md_filename,
                )
                _safe_restore(abs_path, text)
                raise
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

        并发：复用 ``write``/``replace_content`` 同款锁（进程内串行化）；路径安全同
        ``replace_content``——走 ``_safe_abs_path`` realpath 前缀校验，逃逸抛
        ``ValidationError``。
        """
        with self._lock:
            root = self._resolved_root()
            abs_path = self._safe_abs_path(root, md_filename)
            if not os.path.isfile(abs_path):
                logger.debug("md %s not found, skip remove_content", md_filename)
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
                logger.warning(
                    "md block not found in %s (drift suspected)", md_filename
                )
                return False

            # 还原：剩余块间用 \n\n 拼接，末尾补 \n\n（与 replace_content 口径一致，
            # 保证后续 write 追加 / 看门狗按行遍历口径不变）。删空后 out 为空串，
            # 写回空文件（全部块已删，与影子索引全删对齐）。
            out = "".join(f"{b}\n\n" for b in new_blocks)
            # 原子写（见 replace_content 同款）：写失败回写旧全文 ``text`` 恢复调用前状态，
            # 让 delete 补偿路径有机会把已删块插回（回写后 md 恢复调用前，shadow 已删可回滚）。
            try:
                with open(abs_path, "w", encoding="utf-8") as fh:
                    fh.write(out)
            except Exception:
                logger.warning(
                    "md atomic write failed %s, restored from snapshot",
                    md_filename,
                )
                _safe_restore(abs_path, text)
                raise
            return True

    # -- 文件级读写（read_md_file / edit_md_file 存储层落点，F08 §D1）--------- #

    def read_content(
        self,
        scope: Scope,
        md_basename: str,
        *,
        project: str = "",
        line_range: tuple[int, int] | None = None,
    ) -> str:
        """读 md 文件原文，按 ``line_range`` 行切片（越界 clamp，1-based 闭区间）。

        basename + project 经 :meth:`resolve_md_path`（与 :meth:`_md_path` 同口径）映射为
        相对路径，``_safe_abs_path`` 收口为 root 内绝对路径后读全文。``line_range`` 为
        ``None`` 返回整文件；否则按 ``splitlines(keepends=True)`` 切片，``[start-1:end]``
        取行，越界 clamp 到 ``[0, len(lines)]``——``start`` 超界或 ``end <= start`` 返回
        空串。文件不存在返回空串（容错，见契约 :meth:`MarkdownStore.read_content`）。
        """
        with self._lock:
            root = self._resolved_root()
            rel_path = self.resolve_md_path(md_basename, project)
            abs_path = self._safe_abs_path(root, rel_path)
            if not os.path.isfile(abs_path):
                logger.debug("md %s not found, read_content returns empty", rel_path)
                return ""
            with open(abs_path, encoding="utf-8") as fh:
                text = fh.read()
            if line_range is None:
                return text
            start, end = line_range
            lines = text.splitlines(keepends=True)
            # 1-based 闭区间 → 0-based 半开 [start-1, end)，越界 clamp 到 [0, len]。
            lo = max(start - 1, 0)
            hi = min(end, len(lines))
            if lo >= hi:
                return ""
            return "".join(lines[lo:hi])

    def edit_content(
        self,
        scope: Scope,
        md_basename: str,
        old_text: str,
        new_text: str,
        *,
        project: str = "",
    ) -> None:
        """文件级唯一子串替换 + 整文件原子写（F08 §D5 / R5）。

        basename + project 解析路径后，读全文 → ``str.count(old_text)`` 校验唯一性 →
        ``replace(old_text, new_text, 1)`` 精确替换一处 → ``open("w")`` 原子写。与
        :meth:`replace_content` 的差别：本方法做**文件级任意子串替换**（不按 ``\\n\\n``
        切块、不要求 old_text 是整块正文），且**校验唯一性**（不唯一抛 ValidationError，
        非首版「取首个」）。对齐 agent-core edit_memory 契约（old_text 任意文本、跨块均可、
        必须唯一）。块结构重组（合并/拆分）交由上层 ``_supersede_shadow_for_md`` 按块解析
        处理，md 侧只保证文件级替换的原子性与唯一性。

        失败语义：文件不存在 / old_text 未找到抛 ``NotFoundError``；old_text 不唯一抛
        ``ValidationError``；原子写失败经 :func:`_safe_restore` 回写读出的旧全文后重抛
        （与 :meth:`replace_content` 同款补偿，让「异常等价于未发生」对 md 成立）。
        """
        with self._lock:
            root = self._resolved_root()
            rel_path = self.resolve_md_path(md_basename, project)
            abs_path = self._safe_abs_path(root, rel_path)
            if not os.path.isfile(abs_path):
                raise NotFoundError(
                    f"md file not found for edit: {md_basename!r} (project={project!r})"
                )
            with open(abs_path, encoding="utf-8") as fh:
                text = fh.read()

            count = text.count(old_text)
            if count == 0:
                raise NotFoundError(
                    f"old_text not found in {md_basename!r}: {old_text!r}"
                )
            if count > 1:
                raise ValidationError(
                    f"old_text not unique in {md_basename!r} "
                    f"(found {count} occurrences): {old_text!r}"
                )
            out = text.replace(old_text, new_text, 1)

            # 原子写（见 replace_content 同款）：open("w") 截断后 write 抛错会让旧内容
            # 永久丢失——补偿路径（_supersede_shadow_for_md 回滚 shadow）救不回 md。
            # 写失败先回写读出的旧全文 text 恢复调用前状态，再重抛原异常。
            try:
                with open(abs_path, "w", encoding="utf-8") as fh:
                    fh.write(out)
            except Exception:
                logger.warning(
                    "md atomic write failed %s, restored from snapshot",
                    md_basename,
                )
                _safe_restore(abs_path, text)
                raise

    def restore_content(
        self,
        scope: Scope,
        md_basename: str,
        text: str,
        *,
        project: str = "",
    ) -> None:
        """把 md 文件内容精确回写为 ``text``（edit_md_file 的 md 侧失败补偿，见契约）。

        与 :meth:`edit_content` 同口径解析 basename + project 为相对路径 → ``_safe_abs_path``
        收口 → ``_safe_restore`` 整文件覆盖。不做存在性/唯一性/子串校验——补偿必须
        无条件还原成编辑前旧全文，不能因子串歧义再失败。`_safe_restore` 回写失败只记
        warning 不抛，md 残缺漂移交看门狗。
        """
        with self._lock:
            root = self._resolved_root()
            rel_path = self.resolve_md_path(md_basename, project)
            abs_path = self._safe_abs_path(root, rel_path)
            _safe_restore(abs_path, text)

    # -- 路径计算 ------------------------------------------------------------ #

    def _md_path(self, unit: MemoryUnit) -> str:
        """按 F08 §3 映射算 md 文件相对根目录的路径。

        读 memory_class（空兜底 long_term_memory）+ project（system_metadata.project
        标量优先，coords.project 兜底，空落空串）。user_profile 落 root 顶层
        ``USER.md``（跨 project 用户画像，不进 memory/）；无 project 的
        long_term_memory/daily_memory 落 memory 根下，与影子索引
        project 列空串 = 跨项目可见的语义对齐：
        ``memory/MEMORY.md``、``memory/daily_memory/YYYY-MM-DD.md``。
        """
        metadata = unit.system_metadata or {}
        memory_class = self._resolved_memory_class(metadata)
        project = self._project_of(unit)

        # 未知 memory_class 走 long_term_memory 兜底（MEMORY.md）
        sub, into_project = _PATH_MAP.get(memory_class, _PATH_MAP[_DEFAULT_CLASS])

        if not into_project:
            # user_profile：跨 project 用户画像落 root 顶层（不进 memory/，无 project 段）
            return sub

        # long_term_memory / daily_memory：无 project（空串）时落 memory 根下
        # （跨项目可见），有 project 时进 project 子目录。
        if not project:
            if sub == "daily_memory":
                date = datetime.date.today().isoformat()
                return f"memory/daily_memory/{date}.md"
            return f"memory/{sub}"

        if sub == "daily_memory":
            date = datetime.date.today().isoformat()
            return f"memory/{project}/daily_memory/{date}.md"
        # long_term_memory
        return f"memory/{project}/{sub}"

    @staticmethod
    def _resolved_memory_class(metadata: dict) -> str:
        """读 memory_class，空兜底 long_term_memory（MEMORY.md）。

        write 与 _md_path 共用此方法，保证 md 路径算值与回填进 system_metadata
        的值（进而影子索引 category 列）口径一致。
        """
        return str(metadata.get(MEMORY_CLASS_KEY) or "").strip() or _DEFAULT_CLASS

    @staticmethod
    def _project_of(unit: MemoryUnit) -> str:
        """取 project：``system_metadata.project``（agent 场景标量直传）优先，
        ``coords.project``（router 场景 ``_route`` 回写）兜底，空落空串。

        project 不入 KERNEL / TRANSIENT / NON_PROPAGATED 保留集——agent 场景无
        router，coords 是 dict 会被 ``_reject_non_scalar_metadata`` 拒，故 project
        作标量直传 system_metadata，经 ``inherited_system_metadata`` 继承到派生
        unit。两路同 key 取值，与召回侧 shadow ``_PROJECT_FILTER_FIELD`` 口径一致。
        """
        metadata = unit.system_metadata or {}
        project = str(metadata.get("project") or "").strip()
        if project:
            return project
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

        - daily 文件（daily_memory，落 ``daily_memory/日期.md``）：
          标题 = ``coords["team"]``——daily 按天聚合多人多来源的记忆，标题行用 team 名
          标识本条记忆的来源团队；team 坐标缺失时标题留空。
        - 其余文件（USER.md / MEMORY.md）：标题 = ``system_metadata["md_title"]``
          （LLM 抽取时与 tier/tags 同 prompt 生成，见 extractor）；缺失时标题留空
          （infer=false 直写与看门狗重建路径无 LLM 标题，不再用 unit_id 占位）。

        **标题为空时仍写标题行 ``#``（不省略、不用 unit_id 兜底）**：块结构是
        ``# 标题\\n正文\\n\\n`` 两行，replace/remove 按 ``split("\\n", 1)`` 取首行作标题、
        第二行作正文比对——省略标题行会让正文顶到首行被当标题，导致块定位失效。
        空标题行保留块的两行结构，标题内容为空对读回路径零影响（标题行内容本就不被解析）。

        正文是单行（看门狗按行切分前提，F07 §12.4）。块间靠尾部空行分隔。
        标题行在任何读回路径中不被解析（看门狗跳过 ``#`` 行、replace/remove 按正文
        定位块、影子索引不存标题），标题内容变化对机器路径零影响。
        """
        if memory_class == "daily_memory":
            metadata = unit.system_metadata or {}
            coords = metadata.get(COORDS_KEY)
            team = ""
            if isinstance(coords, dict):
                team = str(coords.get("team") or "").strip()
            title = team
        else:
            title = str((unit.system_metadata or {}).get(MD_TITLE_KEY) or "").strip()
        logger.info(
            "[trace/md] render block | title=%r | memory_class=%s | md_title_key=%s | team=%r | unit_id=%s",
            title, memory_class,
            str((unit.system_metadata or {}).get(MD_TITLE_KEY) or ""),
            (coords.get("team") if isinstance(coords, dict) else None) if memory_class == "daily_memory" else None,
            unit.id[:8],
        )
        return f"# {title}\n{content}\n\n"

    # -- 内部 ---------------------------------------------------------------- #

    @staticmethod
    def _safe_abs_path(root: str, md_filename: str) -> str:
        """md_filename → 受限 root 内的绝对路径；逃逸输入抛 ``ValidationError``。

        安全模型（F07 §5 路径安全）：``md_filename`` 可能源于调用方可写的
        ``system_metadata``（replace/remove 从旧 unit 读回、write 由 project
        （system_metadata.project 或 coords.project）参与拼路径），不可信。
        ``os.path.join`` 无防护——``..`` 穿透与绝对路径整体
        替换（join 遇绝对路径丢弃 root）都能逃出 root，故在此统一收口：

        - 绝对路径直接拒绝（防 join 静默丢 root）；
        - ``realpath`` 归一双方后做前缀校验（commonpath），同时消解 ``..``、
          双斜杠与 symlink（软链指向 root 外的文件被解析出真实位置后同样被拒）；
        - Windows 跨盘符 commonpath 抛 ValueError → 视为逃逸拒绝。

        逃逸抛 ``ValidationError`` 而非返 False——False 语义是「md 与索引漂移，
        容忍不报错」，路径逃逸是非法输入，fail-closed（与 _validate_units 同款
        批语义）。已知边界：realpath 校验与 open 之间存在 TOCTOU 窗口，本收口
        面向「恶意 md_filename 字符串」威胁模型，非对抗本地竞争者的沙箱。
        """
        if not md_filename or os.path.isabs(md_filename):
            raise ValidationError(
                f"md_filename must be a relative path under markdown root, got: {md_filename!r}"
            )
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(os.path.join(real_root, md_filename))
        try:
            under_root = os.path.commonpath([real_root, real_path]) == real_root
        except ValueError:
            # Windows 跨盘符（如 root 在 E: 而目标在 C:）——不在同一树内，视为逃逸。
            under_root = False
        if not under_root:
            raise ValidationError(
                f"md_filename escapes markdown root {root!r}: {md_filename!r}"
            )
        return real_path

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
