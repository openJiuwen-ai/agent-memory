"""双实例中期记忆端到端集成测试——验证分布式锁的跨实例互斥语义。

模拟多实例部署：两个 engine 各自的 asyncio.run 在不同线程上并发跑——
真正双事件循环 + 跨线程竞争同一把 Redis 锁。两个 engine 共享同一 Redis
作为 KV 与 lock 后端。

场景：
- 两个 engine 在 barrier 同步下**同时**对**同一 scope** 各自 write 一条不同的 middle 原文；
- 手动构造 MiddleToLongJob 实例并在 barrier 同步下**同时**调 ``job.run()``；
- 期望：只有**一个**实例的 Job 进入临界区跑完 evolver.evolve 把原文 ARCHIVED；
  另一个实例的 Job 取锁失败，返回 ``skipped_due_to_lock=true``；
- 该 scope 的两条候选原文最终**都被 ARCHIVED**（跑完临界区的 Job 在
  ``_list_working_units`` 阶段扫到了两条候选，一起处理 + 一起归档；
  被 skip 的 Job 不重复处理）——证明无重复抽取、无重复归档。

**默认 skip**：依赖真实 Redis 容器（``AGENT_MEMORY_TEST_REDIS_PORT``，默认 6379），
且单次跑 15s+。需要跑时移除下方 ``pytestmark`` 里的 ``pytest.mark.skip``，
并确保 docker redis 在 6379 端口可达（或设 ``AGENT_MEMORY_TEST_REDIS_PORT`` 指向其他端口）。
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid

import pytest

from jiuwen_memory.api.memory_api_impl.assembly import _build_kernel as build_kernel
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.config.config import Config

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skip(
        reason=(
            "e2e 双实例用例默认跳过——耗时 15s+ 且依赖真实 Redis 容器；"
            "需要跑时移除此处的 pytest.mark.skip，并确保 docker redis 在 6379 端口可达"
            "（或设 AGENT_MEMORY_TEST_REDIS_PORT 指向其他端口）"
        )
    ),
]

# 默认 6379（与 F06 文档配置示例一致）；test_integration_backends 默认 6300。
# 用环境变量覆盖以便在两种 docker 端口下都能跑。
REDIS_PORT = int(os.getenv("AGENT_MEMORY_TEST_REDIS_PORT", "6379"))
REDIS_URL = os.getenv("AGENT_MEMORY_TEST_REDIS_URL", f"redis://localhost:{REDIS_PORT}/0")


def _skip_if_redis_unreachable() -> None:
    """Redis 不可达时 skip——与 test_integration_backends 同惯例。"""
    pytest.importorskip("redis")
    import redis

    try:
        redis.Redis(host="localhost", port=REDIS_PORT).ping()
    except Exception as exc:
        pytest.skip(f"redis unreachable on :{REDIS_PORT}: {exc}")


def _cleanup_redis_scope(scope: Scope) -> None:
    """清掉本 scope 在 Redis 上的 KV 与 lock 命名空间——避免跨用例污染。"""
    import redis

    try:
        client = redis.Redis(host="localhost", port=REDIS_PORT)
        # KV 数据键：memory/{id} 与 scope_segments 渲染的命名空间前缀。
        # lock 键：am:lock:v1:{scope 五段}:middle_to_long。
        for pattern in (
            "memory:*",  # KV memory key 前缀
            "am:lock:v1:*",  # lock 前缀
        ):
            for key in client.scan_iter(match=pattern):
                client.delete(key)
    except Exception as exc:  # best-effort teardown——redis 挂时不能让测试爆炸
        import logging
        logging.getLogger(__name__).warning(
            "redis cleanup failed for scope=%s: %s", scope, exc
        )


def _build_engine_config(*, instance_name: str, tick_interval: int = 1) -> Config:
    """装配双实例测试用 Config——单实例最小栈 + AsyncTimerScheduler + Redis KV/lock。

    - engine: in_memory（write 路径与 cloud 同构，且不需要 binding）
    - scheduler: async_timer，tick=1s 让触发尽快发生
    - kv_store: redis，两实例共享同一后端（同 url）
    - lock: redis，两实例各自独立的具名 LockProvider，连同一 Redis 后端 → 锁键一致互斥
    - job_factory.default.params.lock 引用本实例的具名 lock → 注入到 MiddleToLongJob

    **关键设计**：两实例用不同的 lock 具名实例名（``lock_a`` / ``lock_b``），
    避免 ``LockProducer._instances`` 跨线程 race——同进程多线程模拟时，
    Factory 类变量 ``_instances`` 全局共享，两线程并发 ``build_kernel`` 各自
    ``reset_all`` 会清掉对方的具名实例，导致 Job.run 走 ``build_named("default")``
    时拿到对方线程的事件循环上建连的 Redis client，触发
    "Future attached to a different loop"。

    用不同具名实例名让两线程互不污染对方的 ``_instances`` 槽位，各自装配各自
    的 LockProvider，但都连同一 Redis——锁键 ``am:lock:v1:{scope}:middle_to_long``
    仍一致，互斥语义不变。真实多进程部署时不会遇到此 race（每进程独立 Factory）。

    合并语义：AssemblyContext.merged 按 namespace 整体覆盖——
    engine.default.params 必须完整重写所有引用字段。
    """
    config_dict = {
        # Redis KV——两实例共享
        "kv_store": {
            "default": {"target": "redis", "params": {"url": REDIS_URL}}
        },
        # Redis 分布式锁——两实例各自独立具名（避免 _instances race），同 Redis 后端
        "lock": {
            instance_name: {
                "target": "redis",
                "params": {
                    "url": REDIS_URL,
                    "lease_ms": 30000,
                    "wait_timeout_ms": 0,  # 只试一次，与 Job.run 入参一致
                },
            }
        },
        # scheduler——tick=1s 让 Timer 触发尽快发生
        "scheduler": {
            "default": {"target": "async_timer", "params": {"tick_interval": tick_interval}}
        },
        # LLM——echo 桩，回显 prompt 文本；MiddleToLongJob._check_continuity
        # 的正则 fallback 会匹配到 prompt 内首出现的 "true"（prompt 里 "true" 排在
        # "false" 之前），全部判为连续——确保两条候选被合并到一批送 evolver。
        "llm": {
            "default": {"target": "echo", "params": {}}
        },
        # engine.default.params 必须完整重写
        "engine": {
            "default": {
                "target": "in_memory",
                "params": {
                    "ingestor": "default",
                    "index_builder": "default",
                    "retriever": "default",
                    "kv_store": "default",
                    "scheduler": "default",
                    "evolver": "default",
                    "lifecycle": "default",
                    "job_factory": "default",
                },
            }
        },
        # job_factory.default.params：声明 lock 引用本实例的具名 lock，
        # 让 _build_middle_to_long_job_spec 通过 LockProducer.dep(config) 装配
        # LockProvider 注入到 MiddleToLongJob。
        "job_factory": {
            "default": {
                "target": "default",
                "params": {
                    "storage": "default",
                    "evolver": "default",
                    "lifecycle": "default",
                    "index_builder": "default",
                    "llm": "default",
                    "lock": instance_name,  # ← 关键：引用本实例的具名 lock
                    "middle_max_fetch": 100,
                    "middle_batch_size": 10,
                    "middle_concurrency": 1,
                },
            }
        },
    }
    return Config.from_dict(config_dict)


def _run_engine_in_thread(
    instance_name: str,
    *,
    content: str,
    scope: Scope,
    out: dict,
    out_key: str,
    barrier: threading.Barrier | None = None,
    hold_seconds: float = 4.0,
) -> threading.Thread:
    """在独立线程 + 独立事件循环里跑一个 engine。

    每个线程内：
    1. ``build_kernel(config)``——装配独立实例（各自 KV/lock client 连同一 Redis）；
    2. ``await engine.write(...middle=true...)`` 落原文（不 submit Timer，因为 middle_interval
       故意调成 9999s 让 Timer 永不在 hold_seconds 内触发）；
    3. **手动构造 MiddleToLongJob 实例** 并在 barrier 同步下**同时**调 ``await job.run()``——
       两线程真正同时尝试取锁，触发 LockTimeoutError 路径；
    4. 读 JobInfo 看 ``skipped_due_to_lock``。

    用 ``middle_interval=9999`` 屏蔽 Timer 路径——Timer 的 tick 时机与 sleep 时长不确定，
    两个 thread 的 Timer 不会精确同时触发，难以稳定复现锁竞争。
    手动构造 Job 实例 + barrier 同步 ``run()`` 调用是直接验证 Job 层锁互斥的稳定方式。
    Timer 触发链路已在 ``test_middle_e2e.py`` 验证。
    """

    def _worker() -> None:
        try:
            async def _scenario() -> None:
                config = _build_engine_config(instance_name=instance_name)
                kernel = build_kernel(config=config)
                api = kernel.api
                engine = api._engine  # pylint: disable=protected-access

                # write 落原文——middle_interval=9999 让 Timer 永不在 hold_seconds 内触发
                units = await engine.write(
                    content,
                    scope,
                    metadata={
                        "infer": "true",
                        "middle": "true",
                        "middle_interval": "9999",
                    },
                )
                out[f"{out_key}_unit_id"] = units[0].id

                # 取装配好的 JobFactory，构造 MiddleToLongJob 实例——与 Timer 路径
                # 产生的实例同构，只是绕过 Timer 调度。E-06：index/evolver 必须注入
                # engine 装配的同一套实例（与 _write_middle_path 的注入方式一致）。
                job_factory = engine._job_factory  # pylint: disable=protected-access
                from jiuwen_memory.control.jobs import JobType
                job = job_factory.get_job(
                    JobType.MIDDLE_TO_LONG,
                    scope=scope,
                    evolver=engine._evolver,  # pylint: disable=protected-access
                    index=engine._index,  # pylint: disable=protected-access
                )

                # barrier 同步——两线程同时开始 await job.run()
                if barrier is not None:
                    await asyncio.to_thread(barrier.wait)

                result = await job.run()
                out[f"{out_key}_result_detail"] = dict(result.detail)
                if result.detail.get("skipped_due_to_lock") == "true":
                    out[f"{out_key}_skipped"] = True
                    out[f"{out_key}_ran"] = False
                else:
                    out[f"{out_key}_skipped"] = False
                    out[f"{out_key}_ran"] = True

                # 取锁失败的 Job 立即返回，但另一 thread 的 Job 还在临界区内跑
                # evolver / archive——此时读原文 lifecycle 看到的是 ACTIVE 而非 ARCHIVED。
                # 让被 skip 的 Job 等一下，确保另一 Job 的 archive 已落 KV。
                if out[f"{out_key}_skipped"]:
                    await asyncio.sleep(3.0)

                # 读原文 lifecycle——跨线程共享 KV
                got = engine._storage.get(scope, [units[0].id])  # pylint: disable=protected-access
                if got:
                    out[f"{out_key}_lifecycle"] = got[0].lifecycle

                # 清掉 Timer 协程
                scheduler = api._scheduler  # pylint: disable=protected-access
                if hasattr(scheduler, "_wheels"):
                    for wheel in scheduler._wheels.values():  # pylint: disable=protected-access
                        if wheel.task is not None and not wheel.task.done():
                            wheel.task.cancel()

            asyncio.run(_scenario())
        except Exception as exc:  # pragma: no cover
            out[f"{out_key}_error"] = repr(exc)

    t = threading.Thread(target=_worker, name=f"engine-{out_key}")
    t.start()
    return t


# ============================================================ 双实例互斥


def test_two_instances_only_one_runs_middle_to_long() -> None:
    """双实例同 scope 同时写中期记忆 → 只有一个 Job 跑完，另一个 skipped_due_to_lock。

    断言矩阵：
    - 两个 engine 都成功 write（无报错）；
    - 恰好一个 ``ran_count == 1``（其原文 ARCHIVED）；
    - 恰好一个 ``skipped_count == 1``（其原文仍 ACTIVE）；
    - 该 scope 最终原文恰好一条 ARCHIVED + 一条 ACTIVE——证明无重复抽取、无重复归档。
    """
    _skip_if_redis_unreachable()
    # InMemoryEngine 不支持非空 space——这里用空 space + 唯一 user 做隔离。
    # 同一 user 的两实例共享 KV / lock 命名空间，模拟同 scope 的多实例部署。
    scope = Scope(org="itest", user=uuid.uuid4().hex)
    _cleanup_redis_scope(scope)
    try:
        out: dict = {}
        barrier = threading.Barrier(2)

        t1 = _run_engine_in_thread(
            "lock_a", content="alice likes tea", scope=scope,
            out=out, out_key="a", barrier=barrier,
        )
        t2 = _run_engine_in_thread(
            "lock_b", content="bob prefers coffee", scope=scope,
            out=out, out_key="b", barrier=barrier,
        )
        t1.join(timeout=30)
        t2.join(timeout=30)

        # ---- 断言 ----
        assert "a_error" not in out, f"engine a failed: {out.get('a_error')}"
        assert "b_error" not in out, f"engine b failed: {out.get('b_error')}"

        a_ran = out.get("a_ran", False)
        b_ran = out.get("b_ran", False)
        a_skipped = out.get("a_skipped", False)
        b_skipped = out.get("b_skipped", False)

        # 恰好一个 Job 进入临界区跑完，另一个被锁拦下 skip。
        # 「ran_total==1」证明无重复抽取——不会两个 Job 都跑 evolver 把派生写两份。
        # 「skipped_total==1」证明锁确实生效——另一 Job 被 LockTimeoutError 拦下。
        ran_total = int(a_ran) + int(b_ran)
        skipped_total = int(a_skipped) + int(b_skipped)
        assert ran_total == 1, (
            f"期望恰好一个 Job 跑完临界区，实际 ran_total={ran_total} "
            f"(a_ran={a_ran}, b_ran={b_ran}, a_skipped={a_skipped}, b_skipped={b_skipped})"
        )
        assert skipped_total == 1, (
            f"期望恰好一个 Job 被 skipped_due_to_lock，实际 skipped_total={skipped_total}"
        )

        # 两原文 lifecycle：恰好都被 ARCHIVED——跑完临界区的那个 Job 处理了两条候选
        # （_list_working_units 扫到同一 scope 下两条 middle=true），两条都归档；
        # 被 skip 的 Job 不重复处理。这验证了锁的真正目的：避免重复抽取 / 重复归档。
        from jiuwen_memory.common.type_def import LifecycleState

        a_lc = out.get("a_lifecycle")
        b_lc = out.get("b_lifecycle")
        assert a_lc == LifecycleState.ARCHIVED, f"a 原文应 ARCHIVED，实际 a_lc={a_lc}"
        assert b_lc == LifecycleState.ARCHIVED, f"b 原文应 ARCHIVED，实际 b_lc={b_lc}"
    finally:
        _cleanup_redis_scope(scope)
