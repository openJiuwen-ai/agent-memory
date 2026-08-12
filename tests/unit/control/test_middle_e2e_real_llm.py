"""端到端集成测试——真实 LLM 跑通 4 个 engine×scheduler 组合下的 middle 路径。

4 个测试用例覆盖矩阵：
- ``test_in_memory_in_process``：InMemoryEngine + InProcessScheduler
- ``test_in_memory_async_timer``：InMemoryEngine + AsyncTimerScheduler
- ``test_cloud_in_process``：CloudEngine + InProcessScheduler
- ``test_cloud_async_timer``：CloudEngine + AsyncTimerScheduler

每个用例按 4 步骤写记忆，每步用不同人物主语 + 不同事件主题（便于 recall 时按主语区分）：

1. **sync write middle=false**（``api.add`` 同步方法）→ evolver 同步 EXTRACT 派生。
2. **sync write middle=true**（``api.add`` 同步方法）→ 提交 MiddleToLongJob 到 scheduler。
3. **await add_async middle=false** → 与 step 1 同构（同步 EXTRACT 派生）。
4. **await add_async middle=true** → 提交 MiddleToLongJob 到 scheduler，期望后台提取。

**关键差异——scheduler 行为**：
- **InProcessScheduler**：``submit`` 是 async + 内部 ``await job.run()``——submit 即跑完 Job。
  step 2 / step 4 的原文**立即 ARCHIVED**——与 middle=false 表现一致（§2.1 检视意见症状）。
- **AsyncTimerScheduler**：``submit`` 入队 + 起 Timer 协程——Job 不立即跑。
  step 2 / step 4 的原文**先 ACTIVE 后 ARCHIVED**——sleep middle_interval 后 Timer 触发 Job.run
  → evolver EXTRACT 派生 + 归档原文。

**测试函数 async def + @pytest.mark.asyncio**：AsyncTimerScheduler 装配期 ``asyncio.create_task``
启动长期 Timer 协程——依赖事件循环存活。``asyncio.run`` 包一层会关循环，Timer 协程被取消。
pytest-asyncio 提供贯穿测试函数全程的事件循环。

**同步 API 调用推到独立线程**：``api.add`` / ``api.list`` / ``api.search`` 是同步方法（内部
``asyncio.run(self._engine.xxx)``）——在已运行的事件循环里直接调会 RuntimeError。用
``asyncio.to_thread`` 推到独立线程（线程没事件循环，内部 asyncio.run 能跑）——主事件循环不阻塞，
Timer 协程继续转。

环境变量配置（在哪填）：
- 项目根目录 ``.env`` 文件（被 :mod:`dotenv` 自动加载，``.gitignore`` 已忽略）；
- 必填 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``OPENAI_MODEL``，任一为空即 skip。
"""

from __future__ import annotations
# pylint: disable=protected-access  # 测试代码需要访问受保护成员以断言装配链行为

import asyncio
import os

import pytest
from dotenv import load_dotenv

# 模块导入时加载项目根 .env——把 .env 内的 OPENAI_API_KEY 等塞进 os.environ。
# .env 在 .gitignore 内已忽略，不会误提交；缺失也不报错（仅本次测试 skip）。
load_dotenv()

from jiuwen_memory.api.memory_api_impl import build_kernel
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Context, LifecycleState, MemoryTier, Scope, memory_key
from jiuwen_memory.common.type_def.memory_codec import loads

logger = get_logger(__name__)
from jiuwen_memory.config.config import Config

pytestmark = [
    pytest.mark.unit,
    pytest.mark.asyncio,
    pytest.mark.skip(
        reason=(
            "e2e 真实 LLM 用例默认跳过——耗时且依赖外部凭证；"
            "需要跑时移除此处的 pytest.mark.skip，并确认 .env 已配置 "
            "OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL"
            "（_llm_config 仍会做 env 校验）"
        )
    ),
]


# -- 公共配置 ------------------------------------------------------------- #

SCOPE = Scope(org="acme", user="alice")


def _llm_config() -> dict:
    """从环境变量读取真实 LLM 配置——装配期经 LlmProducer 注入 OpenAILLM。

    OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL 任一为空即 skip——
    避免 CI 在无凭证或不完整配置环境下误跑。
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    model = os.environ.get("OPENAI_MODEL", "").strip()
    missing = []
    for name, val in (
        ("OPENAI_API_KEY", api_key),
        ("OPENAI_BASE_URL", base_url),
        ("OPENAI_MODEL", model),
    ):
        if not val:
            missing.append(name)
    if missing:
        pytest.skip(
            f"未配置 {'/'.join(missing)}，跳过真实 LLM e2e 测试"
        )
    return {
        "llm": {
            "default": {
                "target": "openai",
                "params": {
                    "llm_api_key": api_key,
                    "llm_model": model,
                    "llm_base_url": base_url,
                    "llm_temperature": 0.0,
                    "llm_max_tokens": 1024,
                },
            }
        }
    }


def _kernel_config(
    *,
    engine_target: str,
    scheduler_target: str,
    middle_interval: int,
    tick_interval: int | None = None,
) -> Config:
    """装配真实 LLM + 指定 engine/scheduler 的 Config。

    合并语义：``AssemblyContext.merged`` 按 namespace/实例名**整体覆盖**
    （不是 merge params）。故覆盖 ``engine.default`` 时必须**完整重写 params**——
    缺 ``scheduler/evolver/kv_store/...`` 等引用会让 ``SchedulerProducer.dep(config,
    default="in_process")`` 走 fallback。

    Args:
        engine_target: "in_memory" 或 "cloud"
        scheduler_target: "in_process" 或 "async_timer"
        middle_interval: MiddleToLongJob.interval（秒，>= tick_interval）
        tick_interval: AsyncTimerScheduler 的 tick（仅 async_timer 用）
    """
    _default = "default"
    config_dict = _llm_config()
    # scheduler.default.params：in_process 不需要 params；async_timer 只需 tick_interval。
    scheduler_params: dict = {}
    if scheduler_target == "async_timer":
        scheduler_params["tick_interval"] = tick_interval or 2
    config_dict["scheduler"] = {
        "default": {"target": scheduler_target, "params": scheduler_params}
    }
    # engine.default.params 必须完整——合并是整体覆盖而非 merge params。
    config_dict["engine"] = {
        "default": {
            "target": engine_target,
            "params": {
                "ingestor": _default,
                "index_builder": _default,
                "retriever": _default,
                "kv_store": _default,
                "scheduler": _default,  # 引用 scheduler.default
                "evolver": _default,
                "lifecycle": _default,
                "job_factory": _default,
                "middle_interval": middle_interval,
            },
        }
    }
    return Config.from_dict(config_dict)


def _cleanup_scheduler(kernel) -> None:
    """清理 AsyncTimerScheduler 的 Timer 协程——避免跨测试残留。

    InProcessScheduler 无后台协程，本函数对其无操作。
    """
    scheduler = kernel.api._scheduler
    if hasattr(scheduler, "_wheels"):
        for wheel in scheduler._wheels.values():
            if wheel.task is not None and not wheel.task.done():
                wheel.task.cancel()


def _list_via_thread(api, scope: Scope = SCOPE, *, identity: Scope = SCOPE):
    """在 async 测试函数里调同步 api.list——推到独立线程避免 RuntimeError。

    api.list 内部 ``asyncio.run(self._engine.list_with_permission_contexts(...))``——
    在已运行的事件循环里直接调会 RuntimeError。to_thread 推到独立线程，
    线程没事件循环，内部 asyncio.run 能跑。
    """
    return asyncio.to_thread(api.list, scope, identity=identity)


def _recall_via_thread(api, query: str, ctx: Context, *, identity: Scope = SCOPE, top_k: int = 30):
    """在 async 测试函数里调同步 api.search——同 _list_via_thread。"""
    return asyncio.to_thread(api.search, query, ctx, identity=identity, top_k=top_k)


async def _recall_async(kernel, query: str, ctx: Context, *, top_k: int = 30):
    """直接 await engine.recall——跳过 api.search 的 to_thread + asyncio.run 双重开销。

    step 4 立即 recall（middle=true 路径）需在 Timer 触发前完成。api.search 内部
    ``asyncio.run(engine.recall)`` 在子线程跑——双重事件循环切换 + LLM 调用累积 10s+
    延迟，常被 Timer 抢先归档原文。直接 await engine.recall 在主循环跑——仍走 LLM
    embedding/retrieval，但省去切换开销，且与 Timer 协程在同循环协作调度（recall
    await 时 Timer 协程可继续 sleep，不会因切换延迟错过窗口）。
    """
    from jiuwen_memory.retrieval.types import RetrievalQuery
    rq = RetrievalQuery(text=query, top_k=top_k, extensions=dict(ctx.extensions))
    return await kernel.api._engine.recall(ctx.scope, rq)


def _sync_write_via_thread(api, content: str, *, middle: bool, identity: Scope = SCOPE):
    """在 async 测试函数里调同步 api.add——推到独立线程避免 RuntimeError。

    step 1 / step 2 用 sync write 验证同步 API 路径。
    """
    metadata = {
        "infer": "true",
        "_extract_prompt_episodic": _extract_prompt_for_episodic(),
    }
    if middle:
        metadata["middle"] = "true"
    return asyncio.to_thread(
        api.add,
        content,
        SCOPE,
        identity=identity,
        metadata=metadata,
    )


async def _async_write(api, content: str, *, middle: bool, identity: Scope = SCOPE):
    """step 3 / step 4 用 async add_async——直接 await。

    与 _sync_write_via_thread 行为应一致（add 同步方法内部就是 asyncio.run(add_async)），
    但在 async 测试函数里直接 await add_async 避免推到线程。
    """
    metadata = {
        "infer": "true",
        "_extract_prompt_episodic": _extract_prompt_for_episodic(),
    }
    if middle:
        metadata["middle"] = "true"
    return await api.add_async(
        content,
        SCOPE,
        identity=identity,
        metadata=metadata,
    )


def _extract_prompt_for_episodic() -> str:
    """拼装 extractor 的 prompt——让 LLM 把原文改写为第三人称事实陈述。

    DynamicLLMExtractor 缺 prompt 时 fallback KeywordExtractor 会复述原文——
    dedup similarity=1.0 全 NOOP，派生不落盘。故 middle 路径必须带 prompt。
    """
    return (
        "You are extracting structured memories from raw dialogue. "
        "For each source message, produce a concise THIRD-PERSON factual "
        "statement that REPHRASES the content (do NOT copy the original "
        "wording). The extracted statement must differ lexically from the "
        "source while preserving the same fact. "
        "Return a JSON array, one item per source, with keys "
        '"source_id", "content", "confidence" (0..1), "tier" '
        '(use "semantic"), "tags" (list of str, include "extracted"). '
        "source_id must be the unit id from the user message. "
        "Output ONLY the JSON array, no markdown fences, no prose. "
        "Example: [{\"source_id\":\"abc\",\"content\":"
        "\"The user prefers green tea.\",\"confidence\":0.9,"
        "\"tier\":\"semantic\",\"tags\":[\"extracted\"]}]"
    )


# 4 步骤的 content——人物主语 + 不同事件主题（便于 recall 时按主语区分）
_STEP1_CONTENT = "alice likes green tea"          # sync write middle=false
_STEP2_CONTENT = "bob visited kyoto last summer"  # sync write middle=true
_STEP3_CONTENT = "carol works on python projects"  # async write middle=false
_STEP4_CONTENT = "dave enjoys hiking on weekends"  # async write middle=true

# middle_interval 必须大于 step 4 立即 recall 的耗时——recall 走 KeywordFeatureExtractor
# + HashingEmbedder（本地非 LLM 调用），实测约 12-15s（含 to_thread + asyncio.run 双重
# 切换 + 索引扫描）。设 30s 留余量：step 4 写入后 Timer 在 30s 触发，立即 recall 在
# 15s 内完成 → 仍有 15s 余量。
_MIDDLE_INTERVAL_ASYNC_TIMER = 30
_TICK_INTERVAL_ASYNC_TIMER = 2

# 等待 Timer 触发的 sleep 时长——middle_interval=30s + 连续性检测/evolve 约 12-18s；
# 50s 留余量给 drain 跑完。
_TIMER_WAIT_SECONDS = 50


# -- 用例 1: InMemoryEngine + InProcessScheduler ------------------------- #


@pytest.mark.asyncio
async def test_in_memory_in_process() -> None:
    """InMemoryEngine + InProcessScheduler——4 步骤写记忆。

    InProcessScheduler.submit 是 async + 内部 ``await job.run()``——submit 即跑完 Job。
    故 step 2 / step 4 的 middle=true 原文**立即 ARCHIVED**——与 middle=false 表现一致
    （§2.1 检视意见症状：InProcessScheduler + middle 路径退化为同步执行）。

    step 1 / step 3（middle=false）：evolver 同步 EXTRACT 派生 → 派生落 /memory/。
    step 2 / step 4（middle=true）：原文落 /memory/ + tier=WORKING → submit Job 立即跑完
    → evolver EXTRACT 派生 + 原文 ARCHIVED。

    debug 观测：每步 write 后都调 recall + list，打印详细——便于排查派生/原文状态。
    """
    config = _kernel_config(
        engine_target="in_memory",
        scheduler_target="in_process",
        middle_interval=4,
    )
    kernel = build_kernel(config=config)
    api = kernel.api
    try:
        ctx = Context(scope=SCOPE)

        # ---- step 1: sync write middle=false（alice + green tea） ----
        units_s1 = await _sync_write_via_thread(api, _STEP1_CONTENT, middle=False)
        assert len(units_s1) >= 1
        alice_original_id = units_s1[0].id
        # middle=false → 原文落 /messages/，派生落 /memory/ ACTIVE+SEMANTIC
        persisted_s1 = loads(kernel.api._engine._kv.get(SCOPE, memory_key(alice_original_id)))
        # middle=false 原文不在 /memory/（落 /messages/），故 kv.get 应返回 None
        logger.info(f"\n[step 1] alice write id={alice_original_id}")
        # 立即 recall + list
        list_after_s1 = await _list_via_thread(api)
        recall_s1 = await _recall_async(kernel, "alice green tea", ctx)
        logger.info(f"[step 1] list size={len(list_after_s1.items)}")
        for u in list_after_s1.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 1] recall hits={len(recall_s1.items)}")
        for item in recall_s1.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        # 断言：list 应有 alice 派生（middle=false → EXTRACT 派生 ACTIVE+SEMANTIC）
        assert any("alice" in u.content.lower() or "green tea" in u.content.lower()
                   for u in list_after_s1.items), "step 1 后 list 应有 alice 派生"

        # ---- step 2: sync write middle=true（bob + kyoto） ----
        units_s2 = await _sync_write_via_thread(api, _STEP2_CONTENT, middle=True)
        assert len(units_s2) == 1  # 原文
        bob_original_id = units_s2[0].id
        # InProcessScheduler 立即跑完 Job → 原文已 ARCHIVED
        persisted_s2 = loads(kernel.api._engine._kv.get(SCOPE, memory_key(bob_original_id)))
        assert persisted_s2.lifecycle == LifecycleState.ARCHIVED, (
            f"InProcessScheduler step 2 应立即 ARCHIVED 原文 {bob_original_id}，"
            f"got {persisted_s2.lifecycle}"
        )
        assert persisted_s2.tier == MemoryTier.WORKING
        logger.info(f"\n[step 2] bob write id={bob_original_id} lifecycle={persisted_s2.lifecycle.value}")
        # 立即 recall + list
        list_after_s2 = await _list_via_thread(api)
        recall_s2 = await _recall_async(kernel, "bob kyoto", ctx)
        logger.info(f"[step 2] list size={len(list_after_s2.items)}")
        for u in list_after_s2.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 2] recall hits={len(recall_s2.items)}")
        for item in recall_s2.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        # 断言：list 应有 bob 派生（middle=true + InProcessScheduler → 立即 EXTRACT 派生）
        assert any("bob" in u.content.lower() or "kyoto" in u.content.lower()
                   for u in list_after_s2.items), "step 2 后 list 应有 bob 派生"

        # ---- step 3: await add_async middle=false（carol + python） ----
        units_s3 = await _async_write(api, _STEP3_CONTENT, middle=False)
        assert len(units_s3) >= 1
        logger.info(f"\n[step 3] carol write")
        # 立即 recall + list
        list_after_s3 = await _list_via_thread(api)
        recall_s3 = await _recall_async(kernel, "carol python", ctx)
        logger.info(f"[step 3] list size={len(list_after_s3.items)}")
        for u in list_after_s3.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 3] recall hits={len(recall_s3.items)}")
        for item in recall_s3.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        # 断言：list 应有 carol 派生
        assert any("carol" in u.content.lower() or "python" in u.content.lower()
                   for u in list_after_s3.items), "step 3 后 list 应有 carol 派生"

        # ---- step 4: await add_async middle=true（dave + hiking） ----
        units_s4 = await _async_write(api, _STEP4_CONTENT, middle=True)
        assert len(units_s4) == 1
        dave_original_id = units_s4[0].id
        # InProcessScheduler 立即跑完 Job → 原文已 ARCHIVED
        persisted_s4 = loads(kernel.api._engine._kv.get(SCOPE, memory_key(dave_original_id)))
        assert persisted_s4.lifecycle == LifecycleState.ARCHIVED, (
            f"InProcessScheduler step 4 应立即 ARCHIVED 原文 {dave_original_id}，"
            f"got {persisted_s4.lifecycle}"
        )
        logger.info(f"\n[step 4] dave write id={dave_original_id} lifecycle={persisted_s4.lifecycle.value}")
        # 立即 recall + list
        list_after_s4 = await _list_via_thread(api)
        recall_s4 = await _recall_async(kernel, "dave hiking", ctx)
        logger.info(f"[step 4] list size={len(list_after_s4.items)}")
        for u in list_after_s4.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 4] recall hits={len(recall_s4.items)}")
        for item in recall_s4.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        # 断言：list 应有 dave 派生（middle=true + InProcessScheduler → 立即 EXTRACT 派生）
        assert any("dave" in u.content.lower() or "hiking" in u.content.lower()
                   for u in list_after_s4.items), "step 4 后 list 应有 dave 派生"

        # 最终 list 查看记忆——应有 4 步所有派生记忆（middle=true 路径的派生 + middle=false 路径的派生）
        final_list = await _list_via_thread(api)
        # 4 条派生 + bob 原文 + dave 原文 = 6 条（alice/carol 原文落 /messages/ 不进 /memory/）
        assert len(final_list.items) == 6, (
            f"应有 6 条记忆（4 ACTIVE 派生 + bob/dave 原文 ARCHIVED），got {len(final_list.items)}"
        )
        active_units = [u for u in final_list.items if u.lifecycle == LifecycleState.ACTIVE]
        archived_units = [u for u in final_list.items if u.lifecycle == LifecycleState.ARCHIVED]
        assert len(active_units) == 4, (
            f"应有 4 条 ACTIVE 派生，got {len(active_units)}"
        )
        assert len(archived_units) == 2, (
            f"应有 2 条 ARCHIVED 原文（bob/dave），got {len(archived_units)}"
        )
        # ACTIVE 4 条全部是派生——无原文（原文 provenance=[] 且 tier=WORKING，派生 provenance 非空）
        assert all(u.provenance for u in active_units), (
            f"ACTIVE 应全为派生（provenance 非空），got {[u.id[:8] for u in active_units]}"
        )
        # ARCHIVED 2 条是 bob/dave 原文
        archived_ids = {u.id for u in archived_units}
        assert archived_ids == {bob_original_id, dave_original_id}, (
            f"ARCHIVED 应为 bob/dave 原文，got {archived_ids}"
        )
    finally:
        _cleanup_scheduler(kernel)


# -- 用例 2: InMemoryEngine + AsyncTimerScheduler ------------------------ #


@pytest.mark.asyncio
async def test_in_memory_async_timer() -> None:
    """InMemoryEngine + AsyncTimerScheduler——4 步骤写记忆。

    AsyncTimerScheduler.submit 入队 + 起 Timer 协程——Job 不立即跑。

    **sync write 的隐藏限制**：step 2 用 sync write（``api.add``）内部
    ``asyncio.run(add_async)`` 在子线程建临时循环跑——AsyncTimerScheduler.submit
    注册的 Timer 协程被绑到子线程临时循环；临时循环关 → Timer 被取消。
    故 step 2 写入后立即查 bob 原文仍 ACTIVE——Timer 已死，尚未触发 Job。

    step 4 用 ``await api.add_async`` 在主循环驱动——``_submit_timer`` 的 update
    分支检测 ``wheel.task.done()`` 后通过 ``_ensure_timer_task`` 重启 Timer
    （bugfix 修复——修复前此处不重启，导致 middle=true Job 永不执行）。
    Timer 在 ``middle_interval`` 秒后触发 MiddleToLongJob.run——Job 列出所有
    middle=true ACTIVE 原文（bob + dave）一并 EXTRACT + 归档。故 sleep 后
    bob 和 dave 原文均 ARCHIVED。

    step 1 / step 3（middle=false）：与用例 1 同构（同步 EXTRACT 派生）。
    """
    config = _kernel_config(
        engine_target="in_memory",
        scheduler_target="async_timer",
        middle_interval=_MIDDLE_INTERVAL_ASYNC_TIMER,
        tick_interval=_TICK_INTERVAL_ASYNC_TIMER,
    )
    kernel = build_kernel(config=config)
    api = kernel.api
    try:
        ctx = Context(scope=SCOPE)

        # ---- step 1: sync write middle=false（alice + green tea） ----
        units_s1 = await _sync_write_via_thread(api, _STEP1_CONTENT, middle=False)
        assert len(units_s1) >= 1
        logger.info(f"\n[step 1] alice write")
        list_after_s1 = await _list_via_thread(api)
        recall_s1 = await _recall_async(kernel, "alice green tea", ctx)
        logger.info(f"[step 1] list size={len(list_after_s1.items)}")
        for u in list_after_s1.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 1] recall hits={len(recall_s1.items)}")
        for item in recall_s1.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        assert any("alice" in u.content.lower() or "green tea" in u.content.lower()
                   for u in list_after_s1.items), "step 1 后 list 应有 alice 派生"

        # ---- step 2: sync write middle=true（bob + kyoto） ----
        # sync write 在子线程建临时循环跑 add_async——Timer 协程被绑到临时循环，
        # 临时循环关后 Timer 被取消。step 2 立即查原文仍 ACTIVE。
        units_s2 = await _sync_write_via_thread(api, _STEP2_CONTENT, middle=True)
        assert len(units_s2) == 1
        bob_original_id = units_s2[0].id
        persisted_s2_immediate = loads(
            kernel.api._engine._kv.get(SCOPE, memory_key(bob_original_id))
        )
        assert persisted_s2_immediate.lifecycle == LifecycleState.ACTIVE
        assert persisted_s2_immediate.tier == MemoryTier.WORKING
        logger.info(f"\n[step 2] bob write id={bob_original_id[:8]} "
              f"lifecycle={persisted_s2_immediate.lifecycle.value}")
        list_after_s2 = await _list_via_thread(api)
        recall_s2 = await _recall_async(kernel, "bob kyoto", ctx)
        logger.info(f"[step 2] list size={len(list_after_s2.items)}")
        for u in list_after_s2.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 2] recall hits={len(recall_s2.items)}")
        for item in recall_s2.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        # step 2 bob 原文 ACTIVE+WORKING 已建索引，立即 recall 应能召回原文
        assert bob_original_id in {item.unit_id for item in recall_s2.items}, (
            f"step 2 立即 recall 应能召回 bob 原文，got {recall_s2.items}"
        )

        # ---- step 3: await add_async middle=false（carol + python） ----
        units_s3 = await _async_write(api, _STEP3_CONTENT, middle=False)
        assert len(units_s3) >= 1
        logger.info(f"\n[step 3] carol write")
        list_after_s3 = await _list_via_thread(api)
        recall_s3 = await _recall_async(kernel, "carol python", ctx)
        logger.info(f"[step 3] list size={len(list_after_s3.items)}")
        for u in list_after_s3.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 3] recall hits={len(recall_s3.items)}")
        for item in recall_s3.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        assert any("carol" in u.content.lower() or "python" in u.content.lower()
                   for u in list_after_s3.items), "step 3 后 list 应有 carol 派生"

        # ---- step 4: await add_async middle=true（dave + hiking） ----
        # async write 在主循环驱动——update 分支重启 Timer（bugfix），Timer 在
        # middle_interval 秒后触发 MiddleToLongJob.run → 归档 bob + dave 原文。
        units_s4 = await _async_write(api, _STEP4_CONTENT, middle=True)
        assert len(units_s4) == 1
        dave_original_id = units_s4[0].id
        persisted_s4_immediate = loads(
            kernel.api._engine._kv.get(SCOPE, memory_key(dave_original_id))
        )
        assert persisted_s4_immediate.lifecycle == LifecycleState.ACTIVE, (
            f"step 4 立即查应仍 ACTIVE，got {persisted_s4_immediate.lifecycle}"
        )
        logger.info(f"\n[step 4] dave write id={dave_original_id[:8]} "
              f"lifecycle={persisted_s4_immediate.lifecycle.value}")
        # 立即 recall——应召回 step 4 原文（Timer 触发前）。
        # _recall_async 直接 await engine.recall，跳过 to_thread + asyncio.run 双重切换。
        result_s4_immediate = await _recall_async(kernel, "hiking", ctx)
        immediate_ids_s4 = {item.unit_id for item in result_s4_immediate.items}
        logger.info(f"[step 4] recall hits={len(result_s4_immediate.items)}")
        for item in result_s4_immediate.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        assert dave_original_id in immediate_ids_s4, (
            f"step 4 立即 recall 应能召回原文 {dave_original_id}，"
            f"got {immediate_ids_s4}"
        )
        list_after_s4 = await _list_via_thread(api)
        logger.info(f"[step 4] list size={len(list_after_s4.items)}")
        for u in list_after_s4.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")

        # ---- sleep 等 Timer 触发 MiddleToLongJob.run ----
        # middle_interval=30s，首次触发 t≈30s；连续性检测 + 真实 LLM evolve 约需 12-18s；
        # sleep 50s 留余量给 drain 跑完。
        await asyncio.sleep(_TIMER_WAIT_SECONDS)

        # step 2 的 bob 原文——Timer 已死，仍 ACTIVE
        persisted_bob_after = loads(
            kernel.api._engine._kv.get(SCOPE, memory_key(bob_original_id))
        )
        # step 2 的 bob 原文——step 4 async write 重启 Timer 后被一并归档。
        # bugfix 前：sync write 让 Timer 死亡，step 4 async write 不重启 Timer，
        # bob 永远 ACTIVE；bugfix 后（_ensure_timer_task 在 update 分支调用）：
        # step 4 重启 Timer，Timer 触发 MiddleToLongJob 处理 entry（其 kind 是
        # MiddleToLongJob），Job.run 列出所有 middle=true ACTIVE 原文一并归档
        # ——bob + dave 都被 ARCHIVED。
        assert persisted_bob_after.lifecycle == LifecycleState.ARCHIVED, (
            f"step 4 重启 Timer 后 bob 原文 {bob_original_id} 应已 ARCHIVED，"
            f"got {persisted_bob_after.lifecycle}"
        )

        # step 4 的 dave 原文——Timer 在主循环存活，触发 Job 后 ARCHIVED
        persisted_dave_after = loads(
            kernel.api._engine._kv.get(SCOPE, memory_key(dave_original_id))
        )
        assert persisted_dave_after.lifecycle == LifecycleState.ARCHIVED, (
            f"step 4 async write sleep 后原文 {dave_original_id} 应已 ARCHIVED，"
            f"got {persisted_dave_after.lifecycle}"
        )

        # 转换后 recall——dave 原文 ARCHIVED 不召回，但应召回派生记忆
        result_dave_after = await _recall_via_thread(api, "hiking", ctx)
        dave_after_ids = {item.unit_id for item in result_dave_after.items}
        assert dave_original_id not in dave_after_ids, (
            f"sleep 后 dave 原文 {dave_original_id} 应 ARCHIVED 不召回，"
            f"got {dave_after_ids}"
        )

        # 最终 list 查看记忆——应有所有派生记忆（step 1/3 派生 + step 4 派生）
        final_list = await _list_via_thread(api)
        # 4 条派生 + bob 原文 + dave 原文 = 6 条（alice/carol 原文落 /messages/ 不进 /memory/）
        assert len(final_list.items) == 6, (
            f"应有 6 条记忆（4 ACTIVE 派生 + bob/dave 原文 ARCHIVED），got {len(final_list.items)}"
        )
        active_units = [u for u in final_list.items if u.lifecycle == LifecycleState.ACTIVE]
        archived_units = [u for u in final_list.items if u.lifecycle == LifecycleState.ARCHIVED]
        assert len(active_units) == 4, (
            f"应有 4 条 ACTIVE 派生，got {len(active_units)}"
        )
        assert len(archived_units) == 2, (
            f"应有 2 条 ARCHIVED 原文（bob/dave），got {len(archived_units)}"
        )
        # ACTIVE 4 条全部是派生——无原文
        assert all(u.provenance for u in active_units), (
            f"ACTIVE 应全为派生（provenance 非空），got {[u.id[:8] for u in active_units]}"
        )
        # ARCHIVED 2 条是 bob/dave 原文
        archived_ids = {u.id for u in archived_units}
        assert archived_ids == {bob_original_id, dave_original_id}, (
            f"ARCHIVED 应为 bob/dave 原文，got {archived_ids}"
        )
    finally:
        _cleanup_scheduler(kernel)


# -- 用例 3: CloudEngine + InProcessScheduler --------------------------- #


@pytest.mark.asyncio
async def test_cloud_in_process() -> None:
    """CloudEngine + InProcessScheduler——4 步骤写记忆。

    CloudEngine 装配期注入 classifier（LLM），pipeline=None 时退化为单 binding 路径，
    行为与 InMemoryEngine 类似。InProcessScheduler 让 submit 立即跑完 Job——
    step 2 / step 4 的原文立即 ARCHIVED（与用例 1 同构）。
    """
    config = _kernel_config(
        engine_target="cloud",
        scheduler_target="in_process",
        middle_interval=4,
    )
    kernel = build_kernel(config=config)
    api = kernel.api
    try:
        ctx = Context(scope=SCOPE)

        # ---- step 1: sync write middle=false（alice + green tea） ----
        units_s1 = await _sync_write_via_thread(api, _STEP1_CONTENT, middle=False)
        assert len(units_s1) >= 1
        logger.info(f"\n[step 1] alice write")
        list_after_s1 = await _list_via_thread(api)
        recall_s1 = await _recall_async(kernel, "alice green tea", ctx)
        logger.info(f"[step 1] list size={len(list_after_s1.items)}")
        for u in list_after_s1.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 1] recall hits={len(recall_s1.items)}")
        for item in recall_s1.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        assert any("alice" in u.content.lower() or "green tea" in u.content.lower()
                   for u in list_after_s1.items), "step 1 后 list 应有 alice 派生"

        # ---- step 2: sync write middle=true（bob + kyoto） ----
        units_s2 = await _sync_write_via_thread(api, _STEP2_CONTENT, middle=True)
        assert len(units_s2) == 1
        bob_original_id = units_s2[0].id
        # InProcessScheduler 立即跑完 Job → 原文已 ARCHIVED
        persisted_s2 = loads(kernel.api._engine._kv.get(SCOPE, memory_key(bob_original_id)))
        assert persisted_s2.lifecycle == LifecycleState.ARCHIVED, (
            f"CloudEngine + InProcess step 2 应立即 ARCHIVED 原文 {bob_original_id}，"
            f"got {persisted_s2.lifecycle}"
        )
        assert persisted_s2.tier == MemoryTier.WORKING
        logger.info(f"\n[step 2] bob write id={bob_original_id[:8]} "
              f"lifecycle={persisted_s2.lifecycle.value}")
        list_after_s2 = await _list_via_thread(api)
        recall_s2 = await _recall_async(kernel, "bob kyoto", ctx)
        logger.info(f"[step 2] list size={len(list_after_s2.items)}")
        for u in list_after_s2.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 2] recall hits={len(recall_s2.items)}")
        for item in recall_s2.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        # bob 派生应可被 recall（InProcessScheduler 立即跑完 EXTRACT + 归档原文）
        assert any("bob" in i.content.lower() or "kyoto" in i.content.lower()
                   for i in recall_s2.items), "step 2 后 recall 应有 bob 派生"

        # ---- step 3: await add_async middle=false（carol + python） ----
        units_s3 = await _async_write(api, _STEP3_CONTENT, middle=False)
        assert len(units_s3) >= 1
        logger.info(f"\n[step 3] carol write")
        list_after_s3 = await _list_via_thread(api)
        recall_s3 = await _recall_async(kernel, "carol python", ctx)
        logger.info(f"[step 3] list size={len(list_after_s3.items)}")
        for u in list_after_s3.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 3] recall hits={len(recall_s3.items)}")
        for item in recall_s3.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        assert any("carol" in u.content.lower() or "python" in u.content.lower()
                   for u in list_after_s3.items), "step 3 后 list 应有 carol 派生"

        # ---- step 4: await add_async middle=true（dave + hiking） ----
        units_s4 = await _async_write(api, _STEP4_CONTENT, middle=True)
        assert len(units_s4) == 1
        dave_original_id = units_s4[0].id
        # InProcessScheduler 立即跑完 Job → 原文已 ARCHIVED
        persisted_s4 = loads(kernel.api._engine._kv.get(SCOPE, memory_key(dave_original_id)))
        assert persisted_s4.lifecycle == LifecycleState.ARCHIVED, (
            f"CloudEngine + InProcess step 4 应立即 ARCHIVED 原文 {dave_original_id}，"
            f"got {persisted_s4.lifecycle}"
        )
        logger.info(f"\n[step 4] dave write id={dave_original_id[:8]} "
              f"lifecycle={persisted_s4.lifecycle.value}")
        list_after_s4 = await _list_via_thread(api)
        recall_s4 = await _recall_async(kernel, "dave hiking", ctx)
        logger.info(f"[step 4] list size={len(list_after_s4.items)}")
        for u in list_after_s4.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 4] recall hits={len(recall_s4.items)}")
        for item in recall_s4.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        assert any("dave" in u.content.lower() or "hiking" in u.content.lower()
                   for u in list_after_s4.items), "step 4 后 list 应有 dave 派生"

        # 最终 list 查看记忆——应有 4 步所有派生记忆 + bob/dave 原文（ARCHIVED）
        final_list = await _list_via_thread(api)
        # 4 条派生 + bob 原文 + dave 原文 = 6 条（alice/carol 原文落 /messages/ 不进 /memory/）
        assert len(final_list.items) == 6, (
            f"应有 6 条记忆（4 ACTIVE 派生 + bob/dave 原文 ARCHIVED），got {len(final_list.items)}"
        )
        active_units = [u for u in final_list.items if u.lifecycle == LifecycleState.ACTIVE]
        archived_units = [u for u in final_list.items if u.lifecycle == LifecycleState.ARCHIVED]
        assert len(active_units) == 4, (
            f"应有 4 条 ACTIVE 派生，got {len(active_units)}"
        )
        assert len(archived_units) == 2, (
            f"应有 2 条 ARCHIVED 原文（bob/dave），got {len(archived_units)}"
        )
        # ACTIVE 4 条全部是派生——无原文
        assert all(u.provenance for u in active_units), (
            f"ACTIVE 应全为派生（provenance 非空），got {[u.id[:8] for u in active_units]}"
        )
        # ARCHIVED 2 条是 bob/dave 原文
        archived_ids = {u.id for u in archived_units}
        assert archived_ids == {bob_original_id, dave_original_id}, (
            f"ARCHIVED 应为 bob/dave 原文，got {archived_ids}"
        )
    finally:
        _cleanup_scheduler(kernel)


# -- 用例 4: CloudEngine + AsyncTimerScheduler ------------------------- #


@pytest.mark.asyncio
async def test_cloud_async_timer() -> None:
    """CloudEngine + AsyncTimerScheduler——4 步骤写记忆。

    与用例 2 同构——验证 CloudEngine 在 Timer 驱动下 middle 路径也能跑通。

    sync write 的隐藏限制：step 2 sync write 在子线程建临时循环——
    Timer 协程被绑到临时循环，循环关闭后 Timer 取消。step 2 立即查仍 ACTIVE。
    step 4 async write 在主循环驱动，update 分支重启 Timer（bugfix 修复），
    sleep 后 bob + dave 均被 MiddleToLongJob 归档 ARCHIVED。
    """
    config = _kernel_config(
        engine_target="cloud",
        scheduler_target="async_timer",
        middle_interval=_MIDDLE_INTERVAL_ASYNC_TIMER,
        tick_interval=_TICK_INTERVAL_ASYNC_TIMER,
    )
    kernel = build_kernel(config=config)
    api = kernel.api
    try:
        ctx = Context(scope=SCOPE)

        # ---- step 1: sync write middle=false（alice + green tea） ----
        units_s1 = await _sync_write_via_thread(api, _STEP1_CONTENT, middle=False)
        assert len(units_s1) >= 1
        logger.info(f"\n[step 1] alice write")
        list_after_s1 = await _list_via_thread(api)
        recall_s1 = await _recall_async(kernel, "alice green tea", ctx)
        logger.info(f"[step 1] list size={len(list_after_s1.items)}")
        for u in list_after_s1.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 1] recall hits={len(recall_s1.items)}")
        for item in recall_s1.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        assert any("alice" in u.content.lower() or "green tea" in u.content.lower()
                   for u in list_after_s1.items), "step 1 后 list 应有 alice 派生"

        # ---- step 2: sync write middle=true（bob + kyoto） ----
        # sync write 子线程临时循环——Timer 协程被取消，step 2 立即查仍 ACTIVE。
        units_s2 = await _sync_write_via_thread(api, _STEP2_CONTENT, middle=True)
        assert len(units_s2) == 1
        bob_original_id = units_s2[0].id
        persisted_s2_immediate = loads(
            kernel.api._engine._kv.get(SCOPE, memory_key(bob_original_id))
        )
        assert persisted_s2_immediate.lifecycle == LifecycleState.ACTIVE
        assert persisted_s2_immediate.tier == MemoryTier.WORKING
        logger.info(f"\n[step 2] bob write id={bob_original_id[:8]} "
              f"lifecycle={persisted_s2_immediate.lifecycle.value}")
        list_after_s2 = await _list_via_thread(api)
        recall_s2 = await _recall_async(kernel, "bob kyoto", ctx)
        logger.info(f"[step 2] list size={len(list_after_s2.items)}")
        for u in list_after_s2.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 2] recall hits={len(recall_s2.items)}")
        for item in recall_s2.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        # step 2 bob 原文 ACTIVE+WORKING 已建索引，立即 recall 应能召回原文
        assert bob_original_id in {item.unit_id for item in recall_s2.items}, (
            f"step 2 立即 recall 应能召回 bob 原文，got {recall_s2.items}"
        )

        # ---- step 3: await add_async middle=false（carol + python） ----
        units_s3 = await _async_write(api, _STEP3_CONTENT, middle=False)
        assert len(units_s3) >= 1
        logger.info(f"\n[step 3] carol write")
        list_after_s3 = await _list_via_thread(api)
        recall_s3 = await _recall_async(kernel, "carol python", ctx)
        logger.info(f"[step 3] list size={len(list_after_s3.items)}")
        for u in list_after_s3.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")
        logger.info(f"[step 3] recall hits={len(recall_s3.items)}")
        for item in recall_s3.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        assert any("carol" in u.content.lower() or "python" in u.content.lower()
                   for u in list_after_s3.items), "step 3 后 list 应有 carol 派生"

        # ---- step 4: await add_async middle=true（dave + hiking） ----
        units_s4 = await _async_write(api, _STEP4_CONTENT, middle=True)
        assert len(units_s4) == 1
        dave_original_id = units_s4[0].id
        persisted_s4_immediate = loads(
            kernel.api._engine._kv.get(SCOPE, memory_key(dave_original_id))
        )
        assert persisted_s4_immediate.lifecycle == LifecycleState.ACTIVE, (
            f"step 4 立即查应仍 ACTIVE，got {persisted_s4_immediate.lifecycle}"
        )
        logger.info(f"\n[step 4] dave write id={dave_original_id[:8]} "
              f"lifecycle={persisted_s4_immediate.lifecycle.value}")
        # 立即 recall——应召回 step 4 原文（Timer 触发前）。
        # _recall_async 直接 await engine.recall，跳过 to_thread + asyncio.run 双重切换。
        result_s4_immediate = await _recall_async(kernel, "hiking", ctx)
        immediate_ids_s4 = {item.unit_id for item in result_s4_immediate.items}
        logger.info(f"[step 4] recall hits={len(result_s4_immediate.items)}")
        for item in result_s4_immediate.items:
            logger.info(f"  - {item.unit_id[:8]} score={item.score:.3f} content={item.content!r}")
        assert dave_original_id in immediate_ids_s4, (
            f"step 4 立即 recall 应能召回原文 {dave_original_id}，"
            f"got {immediate_ids_s4}"
        )
        list_after_s4 = await _list_via_thread(api)
        logger.info(f"[step 4] list size={len(list_after_s4.items)}")
        for u in list_after_s4.items:
            logger.info(f"  - {u.id[:8]} tier={u.tier.value} lifecycle={u.lifecycle.value} "
                  f"content={u.content!r}")

        # ---- sleep 等 Timer 触发 MiddleToLongJob.run ----
        await asyncio.sleep(_TIMER_WAIT_SECONDS)

        # step 2 bob 原文——step 4 重启 Timer 后被一并归档
        persisted_bob_after = loads(
            kernel.api._engine._kv.get(SCOPE, memory_key(bob_original_id))
        )
        assert persisted_bob_after.lifecycle == LifecycleState.ARCHIVED, (
            f"step 4 重启 Timer 后 bob 原文 {bob_original_id} 应已 ARCHIVED，"
            f"got {persisted_bob_after.lifecycle}"
        )

        # step 4 dave 原文——Timer 在主循环，sleep 后 ARCHIVED
        persisted_dave_after = loads(
            kernel.api._engine._kv.get(SCOPE, memory_key(dave_original_id))
        )
        assert persisted_dave_after.lifecycle == LifecycleState.ARCHIVED, (
            f"step 4 async write sleep 后原文 {dave_original_id} 应已 ARCHIVED，"
            f"got {persisted_dave_after.lifecycle}"
        )

        # 转换后 recall——dave 原文 ARCHIVED 不召回
        result_dave_after = await _recall_via_thread(api, "hiking", ctx)
        dave_after_ids = {item.unit_id for item in result_dave_after.items}
        assert dave_original_id not in dave_after_ids, (
            f"sleep 后 dave 原文 {dave_original_id} 应 ARCHIVED 不召回，"
            f"got {dave_after_ids}"
        )

        # 最终 list 查看记忆——应有所有派生记忆
        final_list = await _list_via_thread(api)
        # 4 条派生 + bob 原文 + dave 原文 = 6 条（alice/carol 原文落 /messages/ 不进 /memory/）
        assert len(final_list.items) == 6, (
            f"应有 6 条记忆（4 ACTIVE 派生 + bob/dave 原文 ARCHIVED），got {len(final_list.items)}"
        )
        active_units = [u for u in final_list.items if u.lifecycle == LifecycleState.ACTIVE]
        archived_units = [u for u in final_list.items if u.lifecycle == LifecycleState.ARCHIVED]
        assert len(active_units) == 4, (
            f"应有 4 条 ACTIVE 派生，got {len(active_units)}"
        )
        assert len(archived_units) == 2, (
            f"应有 2 条 ARCHIVED 原文（bob/dave），got {len(archived_units)}"
        )
        # ACTIVE 4 条全部是派生——无原文
        assert all(u.provenance for u in active_units), (
            f"ACTIVE 应全为派生（provenance 非空），got {[u.id[:8] for u in active_units]}"
        )
        # ARCHIVED 2 条是 bob/dave 原文
        archived_ids = {u.id for u in archived_units}
        assert archived_ids == {bob_original_id, dave_original_id}, (
            f"ARCHIVED 应为 bob/dave 原文，got {archived_ids}"
        )
    finally:
        _cleanup_scheduler(kernel)
