# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DreamingRegistry——注册态演进任务（dreaming）的注册表 + KV 持久化。

F04 D2 调度层设计：注册表 ``(scope五元组, mode) → Job``；interval 定时 /
KV 持久化 / 重启恢复。本类只管**数据**（存取/删除/全量加载），不做调度、
不做鉴权——submit/cancel 归 :class:`~control.scheduler.Scheduler`，授权与
编排归 API 层的 ``DreamingCoordinator``（``api/memory_api_impl/dreaming.py``，
PEP 边界：持续授权与 fan-out 逐桶裁决都在 API 层），candidate → resolver
翻译归 Engine（装配与分发，F04 D3）。

持久化形态：保留 scope（``org=__kernel__, space=dreaming``，不装
MemoryUnit——``loads`` 对非 MemoryUnit 记录返回 None，list_units/scopes()
枚举天然不受污染）下每条注册一个 KV 记录：

- key：``dreaming:v2:{base64url(canonical-json([scope..., mode]))}``，字段值可安全
  包含 ``/`` / ``:`` 等分隔符；读取兼容旧扁平键，下次 save 自动迁移；
- value：JSON（scope/mode/interval/candidate/job_id/channel/created_by
  七字段）。

候选源存 **数据**（dict DSL，:func:`candidate_to_dict` 产物）不存 resolver
对象——重启恢复后装配期重建 resolver、重绑 Job（F04 D3：候选源
必须是数据不是代码）。

持续授权（F04 D7）：``created_by`` 记录**谁以什么身份注册**（actor scope
五元组）——重启恢复与每次 tick 都以它重新走授权判定，不因"注册时曾获准"
而永久豁免。旧版本 entry 无该字段 → 恢复期 fail-closed 跳过。

leader 部署模型（F04 D8）：周期实例启动和注册前须持实例锁，每个 tick 复核；
注册表与内存态 Scheduler 无跨实例事务，失锁实例必须立即停止本地 Timer。

锁有两条互斥路径，按构造注入选择：

- **LockProvider 注入（生产推荐）**：复用 common 层锁原语（Redis 实现跨
  实例互斥），租约 + **自动续租**——健康实例持锁超过初始 TTL 不会被
  第二实例抢占（F04 D8，"TTL 过期双跑"遗留的修复）。acquire / renew /
  release 全部在 registry 自有的"锁看守线程"专属事件循环内执行：Redis
  客户端惰性建连且绑定首次使用的事件循环，而 restore 的调用循环是
  ``asyncio.run`` 临时循环——锁操作必须落在一个长命循环上。续租失败
  （网络分区 / Redis 切主）→ ERROR 日志 + 标记失锁 + 停止续租（锁靠租约
  自然过期，另一实例可接管）。注入的 provider 必须是**独立具名实例**：
  其他消费方（如 MiddleToLongJob 的 guard）若共享同一实例会在各自的
  循环里争抢同一 Redis 连接。
- **未注入（默认，本地单进程）**：KV TTL 锁（``dreaming_instance_lock``
  记录，owner uuid + expires_at）也由看守线程续租，并在每 tick 前复核 owner。
  KV 接口无 CAS，严格多实例竞选仍必须注入 LockProvider。
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from jiuwen_memory.common.errors import ConflictError, NotFoundError
from jiuwen_memory.common.lock import (
    DEFAULT_LEASE_MS,
    LockHandle,
    LockProvider,
    LockTimeoutError,
)
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.type_def import Scope

# 保留 scope：内核自用命名空间，不承载用户 MemoryUnit。org 双下划线前缀与
# 用户组织名空间天然隔离（部署侧组织命名约定避开 __ 前缀即可）。
_DREAMING_SCOPE = Scope(org="__kernel__", space="dreaming")
_KEY_PREFIX = "dreaming:"
_KEY_VERSION = "v2:"
# 实例锁（两条路径共用键名空间）——KV TTL 路径的记录键，不以 _KEY_PREFIX
# 开头，load_all 的 prefix 扫描天然不枚举它；LockProvider 路径的锁名
# 见 _LOCK_NAME。
_LOCK_KEY = "dreaming_instance_lock"
_LOCK_NAME = "instance"
_SCOPE_FIELDS = ("org", "space", "user", "agent", "session")

logger = get_logger(__name__)


@dataclass
class DreamingEntry:
    """一条注册态 dreaming 任务的全部可恢复信息。"""

    scope: Scope
    mode: str  # EvolveMode.value
    interval: int  # 秒；>0（定时任务声明）
    candidate: dict[str, Any] | None  # CandidateSource dict DSL；None=谓词全量源
    job_id: str  # scheduler 定时任务长生命 id
    channel: str  # Channel.value
    # 注册者身份（持续授权锚点）：actor scope 五元组。None 仅存在于旧版本
    # entry——恢复期 fail-closed 跳过，不允许无主定时任务复跑。
    created_by: dict[str, str] | None = None


def actor_to_dict(actor: Scope) -> dict[str, str]:
    """actor scope → 可持久化五元组（created_by 字段形态）。"""
    return {f: getattr(actor, f) or "" for f in _SCOPE_FIELDS}


def actor_from_dict(data: dict[str, str] | None) -> Scope | None:
    """created_by 五元组 → actor scope；None / 空记录 → None。"""
    if not data:
        return None
    if not isinstance(data, dict):
        raise ValueError("created_by 必须是 scope 对象")
    return Scope(**{f: str(data.get(f, "") or "") for f in _SCOPE_FIELDS})


class DreamingRegistry:
    """注册表——KV 持久化的存取件，API 层 DreamingCoordinator 持有并驱动。

    实例锁状态挂在本实例上：恢复进程持锁期间 ``release_instance_lock``
    幂等释放（LockProvider 路径 token CAS / KV 路径 owner 匹配——共享
    KV 的其他进程调用 close 时不得把运行中实例的锁清掉）。
    """

    def __init__(
        self,
        kv: Any,
        *,
        lock: LockProvider | None = None,
        on_lock_lost: Callable[[], None] | None = None,
    ) -> None:
        # kv: KVStore（避免循环导入用 Any）
        self._kv = kv
        # ---- LockProvider 路径（注入时启用；生产多实例推荐 Redis 实现） ---- #
        self._lock = lock
        self._lock_handle: LockHandle | None = None
        # 看守线程的专属事件循环——acquire / renew / release 全部跑在其中
        # （Redis 客户端绑定首次使用的循环，不能跟着 restore 的临时循环走）
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._lock_thread: threading.Thread | None = None
        # run_coroutine_threadsafe 返回的续租 future（concurrent.futures），
        # cancel 会传播取消远端 asyncio 任务
        self._lock_renew_future: Any = None
        # 续租失败标记：失锁后不再释放（token 已易主），close 只做线程清理
        self._lock_lost = False
        self._on_lock_lost = on_lock_lost
        # ---- KV TTL 路径（未注入时的兜底） ---- #
        self._lock_owner: str | None = None
        self._kv_lock_stop: threading.Event | None = None
        self._kv_lock_thread: threading.Thread | None = None

    def set_lock_lost_callback(self, callback: Callable[[], None]) -> None:
        self._on_lock_lost = callback

    @property
    def owns_instance_lock(self) -> bool:
        """当前实例是否仍持有有效 leader 租约。"""
        if self._lock is not None:
            return self._lock_handle is not None and not self._lock_lost
        owner = self._lock_owner
        if owner is None:
            return False
        try:
            raw = self._kv.get(_DREAMING_SCOPE, _LOCK_KEY)
            holder = json.loads(raw.decode("utf-8"))
            return (
                holder.get("owner") == owner
                and float(holder.get("expires_at", 0)) > time.time()
            )
        except (NotFoundError, ValueError, TypeError):
            return False

    def _notify_lock_lost(self) -> None:
        callback = self._on_lock_lost
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:
            logger.exception("dreaming lock-lost callback failed: %s", exc)

    @staticmethod
    def key_of(scope: Scope, mode: str) -> str:
        """无歧义注册键：规范 JSON 的 URL-safe base64。"""
        identity = [*(getattr(scope, f) or "" for f in _SCOPE_FIELDS), str(mode)]
        raw = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return f"{_KEY_PREFIX}{_KEY_VERSION}{encoded}"

    @staticmethod
    def _legacy_key_of(scope: Scope, mode: str) -> str:
        coords = "/".join(getattr(scope, f) or "" for f in _SCOPE_FIELDS)
        return f"{_KEY_PREFIX}{coords}:{mode}"

    def _keys_of(self, scope: Scope, mode: str) -> tuple[str, ...]:
        current = self.key_of(scope, mode)
        legacy = self._legacy_key_of(scope, mode)
        return (current,) if current == legacy else (current, legacy)

    def save(self, entry: DreamingEntry) -> None:
        """写入/覆写一条注册（幂等；同键即换绑 job_id/interval/candidate）。"""
        key = self.key_of(entry.scope, entry.mode)
        payload = {
            "scope": {f: getattr(entry.scope, f) for f in _SCOPE_FIELDS},
            "mode": entry.mode,
            "interval": entry.interval,
            "candidate": entry.candidate,
            "job_id": entry.job_id,
            "channel": entry.channel,
            "created_by": entry.created_by,
        }
        value = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self._kv.exists(_DREAMING_SCOPE, key):
            self._kv.update(_DREAMING_SCOPE, key, value)
        else:
            self._kv.insert(_DREAMING_SCOPE, key, value)
        legacy = self._legacy_key_of(entry.scope, entry.mode)
        if legacy != key and self._kv.exists(_DREAMING_SCOPE, legacy):
            try:
                legacy_entry = self._decode(self._kv.get(_DREAMING_SCOPE, legacy))
            except Exception:
                legacy_entry = None
            if (
                legacy_entry is not None
                and legacy_entry.scope == entry.scope
                and legacy_entry.mode == entry.mode
            ):
                self._kv.delete(_DREAMING_SCOPE, legacy)

    def find(self, scope: Scope, mode: str) -> DreamingEntry | None:
        """读一条注册（不动 KV）——注销"先查再撤"与换绑前取旧值用。"""
        for key in self._keys_of(scope, mode):
            try:
                raw = self._kv.get(_DREAMING_SCOPE, key)
            except NotFoundError:
                continue
            entry = self._decode(raw)
            if entry.scope == scope and entry.mode == mode:
                return entry
        return None

    def remove(self, scope: Scope, mode: str) -> DreamingEntry | None:
        """删除一条注册，返回被删的 entry（未注册返回 None，幂等）。"""
        for key in self._keys_of(scope, mode):
            try:
                raw = self._kv.get(_DREAMING_SCOPE, key)
            except NotFoundError:
                continue
            entry = self._decode(raw)
            if entry.scope != scope or entry.mode != mode:
                continue
            self._kv.delete(_DREAMING_SCOPE, key)
            return entry
        return None

    def load_all(self) -> list[DreamingEntry]:
        """全量加载注册表（重启恢复 / 列表回显用）。

        单条损坏（坏 JSON / 缺关键字段 / 类型不符）→ warning + 跳过，不拖垮
        整批恢复——循环体外跳过意味着 `restore_dreaming` 的逐条 try/except
        接不住本异常（`for ... in load_all()` 在进循环体前完整求值），一条
        损坏记录会让全部任务（含健康的）恢复失败。损坏记录保留在 KV，装配
        修复后下次重启可恢复。
        """
        entries: dict[str, tuple[bool, DreamingEntry]] = {}
        for key, raw in self._kv.scan(_DREAMING_SCOPE, prefix=_KEY_PREFIX):
            try:
                entry = self._decode(raw)
                identity = self.key_of(entry.scope, entry.mode)
                is_current = key == identity
                previous = entries.get(identity)
                if previous is None or (is_current and not previous[0]):
                    entries[identity] = (is_current, entry)
            except Exception as exc:
                logger.warning(
                    "dreaming registry skip bad entry: key=%s error=%s: %s",
                    key, type(exc).__name__, exc,
                )
        return [entry for _, entry in entries.values()]

    # ---- 单实例锁（F04 D8 部署模型） ------------------------------------- #

    def acquire_instance_lock(self, ttl_seconds: float | None = None) -> bool:
        """取恢复实例锁（租约）：他实例持有且未过期 → False。

        写序约定（F04 D8）：**持锁 → restore → 持锁期间进程存活**。同一份注册表
        不允许两个实例并发恢复——内存态 Scheduler 与 KV 注册表之间没有跨实例
        一致性协议，双恢复 = 双跑。锁原语按构造注入选择（见模块 docstring）：
        LockProvider 与 KV TTL 兜底都自动续租；崩溃实例的锁靠 TTL 自然过期。
        """
        if self.owns_instance_lock:
            return True
        if self._lock is not None:
            return self._acquire_via_provider(ttl_seconds)
        return self._acquire_via_kv(
            ttl_seconds if ttl_seconds is not None else DEFAULT_LEASE_MS / 1000
        )

    def release_instance_lock(self) -> None:
        """释放恢复实例锁（优雅关闭路径）。幂等——未持锁 / 锁已易主均无害。

        LockProvider 路径：先停续租再 release（token CAS，他人锁删不掉）；
        失锁（续租失败）后只做线程清理，不释放。KV 路径：owner 匹配删除。
        """
        if self._lock is not None:
            self._release_via_provider()
            return
        self._release_via_kv()

    # ---- LockProvider 路径 ------------------------------------------------ #

    def _acquire_via_provider(self, ttl_seconds: float | None) -> bool:
        # 幂等：二次 acquire 先放掉上一把（含看守线程收尾）
        self._release_via_provider()
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run_keeper() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        thread = threading.Thread(
            target=_run_keeper, name="dreaming-lock-keeper", daemon=True
        )
        thread.start()
        ready.wait(timeout=5)
        try:
            acquire_kwargs: dict[str, int] = {"wait_timeout_ms": 0}
            if ttl_seconds is not None:
                acquire_kwargs["lease_ms"] = int(ttl_seconds * 1000)
            handle = asyncio.run_coroutine_threadsafe(
                self._lock.acquire(_DREAMING_SCOPE, _LOCK_NAME, **acquire_kwargs),
                loop,
            ).result(timeout=15)
        except LockTimeoutError:
            # 他实例持锁未过期——关掉本次看守线程，保持 False（不等待重试）
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            return False
        except Exception:
            # acquire 异常（后端不可达等）——线程收尾后原样上抛，fail-fast
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            raise
        self._lock_loop = loop
        self._lock_thread = thread
        self._lock_handle = handle
        self._lock_lost = False
        # 自动续租：每 lease/3 一次（与 LockProvider.guard 的 _renew_loop 同
        # 节拍约定）。续租失败 → 标记失锁 + 停止续租，锁靠租约自然过期。
        self._lock_renew_future = asyncio.run_coroutine_threadsafe(
            self._renew_ever(handle, handle.lease_ms / 3000.0), loop
        )
        return True

    async def _renew_ever(self, handle: LockHandle, interval_s: float) -> None:
        """续租循环（跑在看守线程的循环内）；失败即退出，不抛出不释放。"""
        while True:
            await asyncio.sleep(interval_s)
            try:
                renewed = await self._lock.renew(handle)
            except Exception as exc:
                logger.warning(
                    "dreaming lock renew error: %s: %s", type(exc).__name__, exc
                )
                renewed = False
            if not renewed:
                self._lock_lost = True
                logger.error(
                    "dreaming instance lock LOST (renew failed) — stop renewing; "
                    "lease expires naturally and another instance may take over"
                )
                self._notify_lock_lost()
                return

    def _release_via_provider(self) -> None:
        handle = self._lock_handle
        if handle is None:
            return  # 从未持锁（如共享 KV 的运维进程 close）——幂等
        loop, thread = self._lock_loop, self._lock_thread
        try:
            if not self._lock_lost:
                # 先停续租（停止供给租约）再释放；失锁路径 token 已易主，
                # 释放无意义（CAS 也会挡住），跳过
                if self._lock_renew_future is not None:
                    self._lock_renew_future.cancel()
                    try:
                        self._lock_renew_future.result(timeout=5)
                    except (
                        concurrent.futures.CancelledError,
                        concurrent.futures.TimeoutError,
                    ):
                        pass
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._lock.release(handle), loop
                    ).result(timeout=10)
                except Exception as exc:
                    # 释放失败仅告警：租约自然过期，语义等同崩溃实例
                    logger.warning(
                        "dreaming lock release error (lease will expire): "
                        "%s: %s", type(exc).__name__, exc,
                    )
        finally:
            self._lock_handle = None
            self._lock_renew_future = None
            self._lock_lost = False
            self._lock_loop = None
            self._lock_thread = None
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=5)

    # ---- KV TTL 路径（未注入 LockProvider 时的兜底） ---------------------- #

    def _acquire_via_kv(self, ttl_seconds: int) -> bool:
        """KV TTL 锁：他实例持有且未过期 → False；无锁 / 过期 / 损坏 → 抢占。"""
        now = time.time()
        try:
            raw = self._kv.get(_DREAMING_SCOPE, _LOCK_KEY)
            holder = json.loads(raw.decode("utf-8"))
            if float(holder.get("expires_at", 0)) > now:
                return False
        except (NotFoundError, ValueError, TypeError):
            pass  # 无锁 / 锁损坏 / 已过期——允许抢占
        owner = str(uuid.uuid4())
        payload = json.dumps(
            {"owner": owner, "expires_at": now + ttl_seconds}
        ).encode("utf-8")
        try:
            if self._kv.exists(_DREAMING_SCOPE, _LOCK_KEY):
                self._kv.update(_DREAMING_SCOPE, _LOCK_KEY, payload)
            else:
                self._kv.insert(_DREAMING_SCOPE, _LOCK_KEY, payload)
        except ConflictError:
            return False
        # KV 接口没有 CAS；写后至少复核 owner。若并发者已覆盖，本实例不得
        # 启动任何 Timer。每 tick 仍会再次检查所有权作为运行期围栏。
        current = json.loads(self._kv.get(_DREAMING_SCOPE, _LOCK_KEY).decode("utf-8"))
        if current.get("owner") != owner:
            return False
        self._lock_owner = owner  # 供 release 匹配（本实例确实持锁）
        self._start_kv_renewal(owner, ttl_seconds)
        return True

    def _start_kv_renewal(self, owner: str, ttl_seconds: int) -> None:
        """本地兜底锁也续租，并在失去 owner 时触发停摆回调。"""
        stop = threading.Event()
        self._kv_lock_stop = stop

        def _renew() -> None:
            while not stop.wait(max(1.0, ttl_seconds / 3.0)):
                try:
                    raw = self._kv.get(_DREAMING_SCOPE, _LOCK_KEY)
                    holder = json.loads(raw.decode("utf-8"))
                    if holder.get("owner") != owner:
                        break
                    holder["expires_at"] = time.time() + ttl_seconds
                    self._kv.update(
                        _DREAMING_SCOPE,
                        _LOCK_KEY,
                        json.dumps(holder).encode("utf-8"),
                    )
                except Exception as exc:
                    logger.error("dreaming KV lock renewal failed: %s", exc)
                    break
            if not stop.is_set():
                self._lock_owner = None
                self._notify_lock_lost()

        thread = threading.Thread(
            target=_renew, name="dreaming-kv-lock-keeper", daemon=True
        )
        self._kv_lock_thread = thread
        thread.start()

    def _release_via_kv(self) -> None:
        """KV 锁释放：owner 匹配删除（本实例未持锁或已被抢占 → 不删）。"""
        stop, thread = self._kv_lock_stop, self._kv_lock_thread
        self._kv_lock_stop = None
        self._kv_lock_thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        if self._lock_owner is None:
            return
        try:
            raw = self._kv.get(_DREAMING_SCOPE, _LOCK_KEY)
            holder = json.loads(raw.decode("utf-8"))
        except (NotFoundError, ValueError, TypeError):
            self._lock_owner = None
            return  # 无锁 / 锁损坏——无事可做
        if holder.get("owner") != self._lock_owner:
            self._lock_owner = None
            return  # 锁已被抢占——不是自己的，不删
        try:
            self._kv.delete(_DREAMING_SCOPE, _LOCK_KEY)
        except NotFoundError:
            pass  # 竞态：已被人删（幂等）
        finally:
            self._lock_owner = None

    @staticmethod
    def _decode(raw: bytes) -> DreamingEntry:
        """KV 原始字节 → DreamingEntry（损坏记录跳过由调用方兜底，这里信任）。"""
        payload = json.loads(raw.decode("utf-8"))
        created_by = payload.get("created_by")
        if created_by is not None and not isinstance(created_by, dict):
            raise ValueError("created_by 必须是 scope 对象")
        return DreamingEntry(
            scope=Scope(**{f: payload["scope"].get(f, "") for f in _SCOPE_FIELDS}),
            mode=payload["mode"],
            interval=int(payload["interval"]),
            candidate=payload.get("candidate"),
            job_id=payload["job_id"],
            channel=payload.get("channel", "background"),
            created_by=created_by,
        )
