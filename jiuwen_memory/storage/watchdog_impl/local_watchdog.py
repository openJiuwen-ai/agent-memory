"""Watchdog 本地文件后端实现：监听 md 变更同步影子索引。

借 ``watchdog`` 库的 Observer 监听 memory 目录，文件修改事件经
``loop.call_soon_threadsafe`` 从 Observer 线程投递到主事件循环，
``asyncio.create_task`` 起异步同步任务，sqlite 操作经 ``asyncio.to_thread``
推到独立线程避免阻塞事件循环（桥接模式抄 ``agent-core/lite/manager.py``，
F07 §12.8 方案 B 变体）。

**与 lite 的关键差异**：lite 的 ``sync`` 是文件级全量重建（整文件 chunks 删重建）；
本实现是 **unit 粒度增量**——``shadow.list_units_by_md`` 拿旧 (unit_id, content_hash)
list，读 md 按行算新 content_hash list，diff 出「新增/删除」集合只动变化的 unit。
否则改一行重建整个文件的 unit 会丢失其他 unit 的 id（破坏 supersedes 链，F07 §12）。

**块格式对齐** ``MarkdownStore._render_block``：``# {标题}\\n{content}\\n\\n``
（标题按 F08 §8 分流：daily 文件用 coords.team、其余用 LLM 生成的 md_title、均兜底
unit.id——标题内容对 diff 无影响，本实现只认 ``#`` 开头的标题行）。按行遍历：标题行
（``#`` 开头）跳过、空行跳过、正文行算 sha256（F07 §12.4 单段
unit 约束——一行正文 = 一个 content = 一个 hash 候选）。

**unit_id 策略**：用户改某行 content → 旧 hash 消失 + 新 hash 出现 → 删旧 unit
+ 建新 unit（新 id），不保留旧 id（F07 §12.9 风险5 当前版本策略）。删旧后用旧 id
查询返回空不报错（``shadow.get_units`` 缺失 id 静默省略）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import (
    COORDS_KEY,
    MD_FILENAME_KEY,
    MEMORY_CLASS_KEY,
    MemoryTier,
    MemoryUnit,
    Modality,
    Scope,
    Segment,
    Temporal,
)
from jiuwen_memory.storage.markdown import MarkdownProducer, MarkdownStore
from jiuwen_memory.storage.shadow import DocumentShadowIndex, ShadowIndexProducer
from jiuwen_memory.storage.sync_gate import (
    MAX_SYNC_DEFERRAL_SECONDS,
    SYNC_DEFERRAL_POLL_SECONDS,
    write_window_open,
)
from jiuwen_memory.storage.watchdog import Watchdog, WatchdogProducer

logger = get_logger(__name__)

# md 路径 → memory_class 反推映射（与 LocalMarkdownStore._PATH_MAP 对称，F08 §3）。
# _PATH_MAP: user_memory→USER.md / project_memory→MEMORY.md / team_memory→daily_memory。
# 反推：按文件名/子目录判 category——daily_memory/ 下 → team_memory，MEMORY.md →
# project_memory，USER.md → user_memory。
_FILE_NAME_TO_CLASS: dict[str, str] = {
    "USER.md": "user_memory",
    "MEMORY.md": "project_memory",
}

_DEFAULT_TIER = MemoryTier.SEMANTIC
_DEFAULT_DEBOUNCE_MS = 2000
_INIT_GRACE_SECONDS = 1.0  # 启动后延迟置 watcher_initialized，避开初始扫描风暴


def _content_hash(content: str) -> str:
    """content 整段 sha256，与影子索引 ``_content_hash`` 同口径（sqlite_shadow_index.py）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _project_from_md_path(md_filename: str) -> str:
    """从 md 相对路径反推 project（F08 §3 路径规则）。

    路径形如 ``memory/{project}/daily_memory/2026-09-01.md`` 或
    ``memory/{project}/MEMORY.md``；``USER.md`` 在 memory 根下无 project 段 → ``default``。
    """
    parts = md_filename.replace("\\", "/").split("/")
    # 期望 ["memory", <project?>, ...]；USER.md 形如 memory/USER.md（无 project 段）
    if len(parts) >= 2 and parts[0] == "memory" and parts[1] != "USER.md":
        return parts[1]
    return "default"


def _class_from_md_path(md_filename: str) -> str:
    """从 md 相对路径反推 memory_class（F08 §3 映射，对称 LocalMarkdownStore._PATH_MAP）。"""
    normalized = md_filename.replace("\\", "/")
    if "daily_memory/" in normalized:
        return "team_memory"
    base = os.path.basename(normalized)
    if base in _FILE_NAME_TO_CLASS:
        return _FILE_NAME_TO_CLASS[base]
    # 未知路径兜底 team_memory（与 LocalMarkdownStore._DEFAULT_CLASS 一致）
    return "team_memory"


class LocalWatchdog(Watchdog):
    """本地文件 md 看门狗：Observer 监听 → 桥接主循环 → unit 粒度增量同步。

    构造期注入 shadow + markdown + root + scope + loop + debounce；``start`` 在
    事件循环就绪后调（实现内 ``asyncio.get_running_loop`` 取 loop——构造期 loop
    参数为 None 时 fallback 到 start 时取）。
    """

    def __init__(
        self,
        shadow: DocumentShadowIndex,
        md_store: MarkdownStore,
        markdown_root: str,
        scope: Scope,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        debounce_ms: int = _DEFAULT_DEBOUNCE_MS,
    ) -> None:
        self._shadow = shadow
        self._md_store = md_store
        self._markdown_root = markdown_root
        self._scope = scope
        self._loop = loop
        self._debounce = debounce_ms / 1000.0
        # 观察者句柄与在途定时器（按 md_filename 分键：多文件并发改动互不吞——
        # 单一 timer 时 B 文件的事件会 cancel 掉 A 文件待执行的同步，A 的改动
        # 永久丢失（看门狗纯事件驱动，无新事件不会再补））
        self._observer: Any = None  # watchdog.observers.api.BaseObserver
        self._watch_timers: dict[str, asyncio.TimerHandle] = {}
        self._watcher_initialized = False
        self._closed = False

    # -- Watchdog 契约 ------------------------------------------------------- #

    @property
    def watcher_initialized(self) -> bool:
        """初始宽限期是否已过（公开给 _MdFileHandler 读取，避免跨对象访问保护成员）。"""
        return self._watcher_initialized

    def schedule_sync(self, abs_path: str, *, deleted: bool = False) -> None:
        """Observer 线程回调入口：把同步任务投递到主事件循环（公开给 _MdFileHandler）。"""
        self._schedule_sync(abs_path, deleted=deleted)

    def start(self) -> None:
        """启动文件监听。

        在事件循环线程调。``self._loop`` 未在构造期注入时，此处取
        ``asyncio.get_running_loop``（故 ``start`` 必须在 async 上下文/事件循环线程调）。
        Observer 监听 ``{root}/memory/`` 递归；启动后延迟 ``_INIT_GRACE_SECONDS``
        置 ``_watcher_initialized``，避开 Observer 刚起时对存量文件的初始扫描风暴
        （存量 md 是写入流程产物，不应被看门狗当作"用户手改"触发同步——抄 lite）。
        """
        if self._observer is not None:
            return  # 幂等：已启动
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as exc:
            raise RuntimeError(
                "watchdog 库未安装；文档看门狗需 `pip install watchdog>=4.0`（deploy extras）"
            ) from exc

        class _MdFileHandler(FileSystemEventHandler):
            def __init__(self, owner: "LocalWatchdog") -> None:
                super().__init__()
                self._owner = owner

            def on_modified(self, event: Any) -> None:
                if event.is_directory:
                    return
                if not self._owner.watcher_initialized:
                    return
                if not event.src_path.endswith(".md"):
                    return
                self._owner.schedule_sync(event.src_path)

            def on_created(self, event: Any) -> None:
                # 新建文件等同于"全量新行"——走同一同步路径（old 为空 → 全是 insert）
                if event.is_directory or not self._owner.watcher_initialized:
                    return
                if not event.src_path.endswith(".md"):
                    return
                self._owner.schedule_sync(event.src_path)

            def on_deleted(self, event: Any) -> None:
                # 文件删除 → 该文件所有 unit 全删
                if event.is_directory or not self._owner.watcher_initialized:
                    return
                if not event.src_path.endswith(".md"):
                    return
                self._owner.schedule_sync(event.src_path, deleted=True)

        watch_dir = os.path.join(self._markdown_root, "memory")
        if not os.path.isdir(watch_dir):
            # memory 目录尚不存在：监听根目录，用户创建 memory 后事件自然来。
            # 不能 schedule 不存在的目录（Observer 会报错），退监听 markdown_root 本身。
            watch_dir = self._markdown_root
            if not os.path.isdir(watch_dir):
                os.makedirs(watch_dir, exist_ok=True)

        self._observer = Observer()
        handler = _MdFileHandler(self)
        self._observer.schedule(handler, watch_dir, recursive=True)
        self._observer.start()

        # 延迟置 initialized：避开初始扫描风暴（lite 用 call_later(1.0)）
        self._watcher_initialized = False
        if self._loop is not None:
            self._loop.call_later(_INIT_GRACE_SECONDS, self._set_watcher_initialized)
        logger.info(
            "LocalWatchdog started: watching %s (recursive=True), debounce=%.2fs",
            watch_dir, self._debounce,
        )

    def stop(self) -> None:
        """停止文件监听 + join Observer；幂等。取消在途 debounce 定时器（全部文件）。"""
        self._closed = True
        for timer in self._watch_timers.values():
            timer.cancel()
        self._watch_timers.clear()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5.0)
            except Exception as exc:  # pragma: no cover - 防御性清理
                logger.warning("LocalWatchdog: observer stop/join failed: %s", exc)
            self._observer = None
        self._watcher_initialized = False
        logger.info("LocalWatchdog stopped")

    def health(self) -> None:
        from jiuwen_memory.common.errors import HealthCheckError

        if self._observer is None and not self._closed:
            raise HealthCheckError("LocalWatchdog not started")
        if self._closed:
            raise HealthCheckError("LocalWatchdog is closed")
        return None

    # -- 内部：Observer 线程 → 主循环 桥接 ----------------------------------- #

    def _set_watcher_initialized(self) -> None:
        """延迟置 initialized（避开初始扫描风暴，抄 lite）。"""
        self._watcher_initialized = True
        logger.debug("LocalWatchdog: watcher initialized (post grace period)")

    def _schedule_sync(self, abs_path: str, *, deleted: bool = False) -> None:
        """Observer 线程回调入口：把同步任务投递到主事件循环。

        经 ``loop.call_soon_threadsafe`` 跨线程投递（Observer 线程 → 主循环）。
        debounce 按 md_filename 分键（"取消该文件旧 timer 再起新的"）——同文件
        短时间多次保存合并成一次执行，多文件并发改动互不吞（抄 lite 的
        schedule_watch_sync 思路，分键是其缺陷修正）。
        """
        if self._closed or self._loop is None:
            return
        md_filename = self._to_rel_path(abs_path)
        if md_filename is None:
            return  # 不在 memory 目录下，忽略
        # 携带 deleted 标记：把 (md_filename, deleted) 投递
        try:
            self._loop.call_soon_threadsafe(
                self._arm_timer, md_filename, deleted
            )
        except RuntimeError as exc:
            # 事件循环已关闭（关闭竞态）——静默跳过
            logger.debug("LocalWatchdog: loop closed, skip sync: %s", exc)

    def _arm_timer(self, md_filename: str, deleted: bool) -> None:
        """主循环侧：取消该文件旧 timer，起 debounce 后的新同步任务。

        **按 md_filename 分键**：同一文件多次事件 → 取消该文件的旧 timer 重排 →
        只剩最后一次的 timer 触发（debounce 合并语义不变）；**另一文件的事件
        不再波及**——单一 timer 时 B 文件的 cancel 会吞掉 A 文件待执行的同步，
        而 A 无新事件不会再补（看门狗纯事件驱动），改动永久丢失。
        deleted 标记随该文件最后一次事件携带（文件最终删了就删）。
        """
        if self._closed:
            return
        old = self._watch_timers.get(md_filename)
        if old is not None:
            old.cancel()
        self._watch_timers[md_filename] = self._loop.call_later(
            self._debounce, self._fire_sync, md_filename, deleted
        )

    def _fire_sync(self, md_filename: str, deleted: bool) -> None:
        """debounce 到期：起异步同步任务（清掉该文件的 timer 槽位）。"""
        # 只清自己的槽位：dict.get 防御「timer 触发前被 _arm_timer 重排又恰好
        # 是同一句柄」之外的错位（stop 后残留句柄触发时 _closed 已挡）。
        if self._watch_timers.get(md_filename) is not None:
            del self._watch_timers[md_filename]
        if self._closed:
            return
        asyncio.create_task(self._do_sync(md_filename, deleted))

    async def _do_sync(self, md_filename: str, deleted: bool) -> None:
        """异步同步主体：sqlite 操作推到独立线程（to_thread）避免阻塞事件循环。

        ``_sync_one`` 是同步函数（调 shadow 的同步 sqlite 方法），靠 ``to_thread``
        推出事件循环；shadow 自带 ``threading.Lock`` 保证不并发跑同文件。

        **写窗口推迟（F07 §12.9 风险 6，见 storage.sync_gate）**：正常写入路径
        （CompositeStorage 文档路径两步写：md 视图 + 影子索引）进行中时，md 与
        索引短暂不一致，此刻对账会误判漂移（add 窗口 → 幽灵 unit 双写；update/
        delete 反序窗口 → 删真 unit / 复活已删 unit）。窗口开着时**推迟**而非丢弃：
        轮询等窗口关闭后照常 sync——窗口内用户真实手改 md 的漂移，重扫能补上；
        超过 ``MAX_SYNC_DEFERRAL_SECONDS``（写路径 bug 永不关窗）放弃本次，下一个
        md 文件事件会重新触发。
        """
        try:
            deferred = 0.0
            while write_window_open():
                if deferred >= MAX_SYNC_DEFERRAL_SECONDS:
                    logger.warning(
                        "LocalWatchdog: write window open for %.0fs, give up this sync "
                        "for %s (next md event will re-trigger)",
                        deferred, md_filename,
                    )
                    return
                await asyncio.sleep(SYNC_DEFERRAL_POLL_SECONDS)
                deferred += SYNC_DEFERRAL_POLL_SECONDS
            await asyncio.to_thread(self._sync_one, md_filename, deleted)
        except Exception as exc:
            logger.warning(
                "LocalWatchdog: sync failed for %s: %s", md_filename, exc
            )

    # -- 内部：同步逻辑（在 to_thread 线程跑） ------------------------------- #

    def _to_rel_path(self, abs_path: str) -> str | None:
        """绝对路径 → 相对 markdown_root 的路径（与 shadow 的 md_filename 列同口径）。

        不在 root 下的路径返回 None（忽略）。用 ``os.path.relpath`` 归一，
        返回值含 ``memory/...`` 前缀，与 ``LocalMarkdownStore._md_path`` 产出一致。
        """
        try:
            rel = os.path.relpath(abs_path, self._markdown_root)
        except ValueError:
            # Windows 跨盘符 relpath 报错
            return None
        # 越界（含 ..）忽略
        if rel.startswith(".."):
            return None
        return rel.replace("\\", "/")

    def _sync_one(self, md_filename: str, deleted: bool) -> None:
        """单文件 unit 粒度增量同步（F07 §12.3 流程）。

        ① old = shadow.list_units_by_md(scope, md_filename)  # [(unit_id, content_hash)]
        ② 读 md 按行算 new (content_hash, content) list  (deleted=True 时 new 为空 → 全删)
        ③ diff：
           old_hash - new_hash → delete_units(对应 unit_id)
           new_hash - old_hash → insert_units(新建 unit，步骤 3 缺省元数据 + content 原文)
        """
        scope = self._scope
        # ① 旧 unit（含 content_hash）
        old_pairs = self._shadow.list_units_by_md(scope, md_filename)
        old_by_hash: dict[str, str] = {}
        for unit_id, ch in old_pairs:
            old_by_hash[ch] = unit_id
        old_hash_set = set(old_by_hash.keys())

        # ② 新 (content_hash, content) list（按行遍历 md，保留 content 原文供建 unit）
        new_pairs: list[tuple[str, str]] = []  # (hash, content) 保序、去重
        new_hash_set: set[str] = set()
        if not deleted:
            abs_path = os.path.join(self._markdown_root, md_filename)
            if os.path.isfile(abs_path):
                new_pairs = self._collect_new_contents(abs_path)
                new_hash_set = {h for h, _ in new_pairs}

        # ③ diff
        to_delete = old_hash_set - new_hash_set
        to_insert_pairs = [(h, c) for h, c in new_pairs if h not in old_hash_set]

        if not to_delete and not to_insert_pairs:
            return  # md 与索引一致，无需同步

        # delete 分支
        if to_delete:
            del_ids = [old_by_hash[h] for h in to_delete]
            self._shadow.delete_units(scope, del_ids)
            logger.info(
                "LocalWatchdog: deleted %d unit(s) from %s (md changed/removed)",
                len(del_ids), md_filename,
            )

        # insert 分支：建新 unit（content 原文来自 new_pairs）
        if to_insert_pairs:
            units = [
                self._build_unit(md_filename, content)
                for _, content in to_insert_pairs
            ]
            self._shadow.insert_units(scope, units)
            logger.info(
                "LocalWatchdog: inserted %d unit(s) into %s (md new content)",
                len(units), md_filename,
            )

    def _build_unit(self, md_filename: str, content: str) -> MemoryUnit:
        """建新 MemoryUnit（F07 §12.6 缺省元数据）。

        缺省值：
        - scope.project：从 md 路径反推
        - category(memory_class)：从 md 路径反推（USER.md→user_memory 等）
        - tier：SEMANTIC（与抽取产出的语义记忆一致）
        - temporal：t_ingest=now，其余 None
        - provenance：标记 "watchdog_sync"
        - md_filename：回填进 system_metadata（影子索引 insert_units 从此读）
        """
        project = _project_from_md_path(md_filename)
        memory_class = _class_from_md_path(md_filename)
        now = datetime.now(timezone.utc)
        unit_id = str(uuid.uuid4())
        return MemoryUnit(
            id=unit_id,
            scope=self._scope,
            tier=_DEFAULT_TIER,
            segments=[Segment(content=content, source=Modality.TEXT)],
            source_ref=f"watchdog:{md_filename}",
            temporal=Temporal(
                t_event=None,
                t_ingest=now,
                t_valid=now,
                t_invalid=None,
                t_message=None,
            ),
            provenance=["watchdog_sync"],
            system_metadata={
                COORDS_KEY: {"project": project},
                MEMORY_CLASS_KEY: memory_class,
                MD_FILENAME_KEY: md_filename,
            },
        )

    def _collect_new_contents(
        self, abs_path: str
    ) -> list[tuple[str, str]]:
        """读 md 按行遍历，返回 (content_hash, content) 列表（保序、去重）。

        供 ``_sync_one`` insert 分支建新 unit 时还原 content 原文——
        ``_build_unit`` 需要 content 而非 hash。
        """
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        with open(abs_path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.rstrip("\n")
                if stripped.startswith("#"):
                    continue
                if not stripped.strip():
                    continue
                ch = _content_hash(stripped)
                if ch not in seen:
                    seen.add(ch)
                    result.append((ch, stripped))
        return result


# -- 注册到 WatchdogProducer（实现自注册，新增无需改 producer/装配入口） -------- #


@WatchdogProducer.register("watchdog")
def _build(config):
    """从装配 ComponentConfig 构造 LocalWatchdog。

    跨 namespace 读 markdown_store.default.root 与 shadow_index 实例（与
    ``SqliteDocumentShadowIndex._build`` 跨 namespace 读 markdown root 同手法）。
    ``loop`` 不在此注入——``start`` 时由 ``asyncio.get_running_loop`` 取（装配期
    无 running loop）。
    """
    ctx = config.ctx
    shadow = ShadowIndexProducer.build_named("default", ctx)
    # markdown_store 实例（同上）
    md_store = MarkdownProducer.build_named("default", ctx)
    # markdown root：跨 namespace 读 markdown_store.default.params.root
    markdown_root = None
    md_spec = ctx.lookup("markdown_store", "default")
    markdown_root = (md_spec.params or {}).get("root") or ""
    markdown_root = markdown_root or "."
    debounce_ms = int(Factory.cfg_get(config, "debounce_ms", _DEFAULT_DEBOUNCE_MS))
    # scope：看门狗不按 scope 隔离（影子索引靠 project+category，不走 Scope 字段，
    # 见 sqlite_shadow_index.list_units_by_md 注释），用空 Scope 占位。
    scope = Scope()
    return LocalWatchdog(
        shadow=shadow,
        md_store=md_store,
        markdown_root=markdown_root,
        scope=scope,
        debounce_ms=debounce_ms,
    )
