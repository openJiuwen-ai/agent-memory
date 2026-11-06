"""实例内可信来源账本；只保存引用，不保存记忆正文或可还原片段。"""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from itertools import islice

from ._validation import has_exact_type
from .runtime import Actor, SecurityDenied


def _identifier(value, *, empty=False):
    if not has_exact_type(value, str) or len(value) > 512:
        raise SecurityDenied("来源标识缺失或无效")
    elif not value and (not empty):
        raise SecurityDenied("来源标识缺失或无效")
    return value


def strongest(*levels):
    if not levels or any(level not in {"S1", "S2", "S3"} for level in levels):
        raise SecurityDenied("来源等级未知")
    return max(levels, key={"S1": 1, "S2": 2, "S3": 3}.__getitem__)


@dataclass(frozen=True)
class SourceReference:
    provider: str
    # 插件安全域 org / space / user / agent / session。jiuwen 从实际 MemoryUnit
    # 提取，其他后端提供可信映射；不要求其业务 API 使用相同 Scope 参数。
    scope: tuple[str, str, str, str, str] = field(repr=False)
    unit_id: str = field(repr=False)
    revision: str = field(repr=False)
    level: str
    purpose: str
    presentation_version: str = "external-1"
    snapshot_digest: str = field(default="external-1", repr=False)

    def __post_init__(self):
        for value in (
            self.provider,
            self.unit_id,
            self.revision,
            self.purpose,
            self.presentation_version,
            self.snapshot_digest,
        ):
            _identifier(value)
        if not has_exact_type(self.scope, tuple) or len(self.scope) != 5:
            raise SecurityDenied("记忆来源范围缺失")
        for index, value in enumerate(self.scope):
            _identifier(value, empty=index != 0)
        strongest(self.level)


@dataclass(frozen=True)
class SourceSnapshot:
    actor: Actor
    level: str
    version: str
    references: tuple[SourceReference, ...] = field(repr=False)


@dataclass
class _State:
    references: frozenset[SourceReference] = field(default_factory=frozenset, repr=False)
    level: str = "S1"
    version: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    active: int = 0


class SourceLedger:
    """begin 只能用于宿主确认全新的上下文；旧 checkpoint 必须已有可信来源状态。"""

    def __init__(self, *, capacity=128, reference_capacity=4096):
        if any(not has_exact_type(n, int) or n < 1 for n in (capacity, reference_capacity)):
            raise ValueError("来源容量必须是正整数")
        self.capacity, self.reference_capacity = capacity, reference_capacity
        self._states: dict[Actor, _State] = {}
        self._retired: set[Actor] = set()
        self._validators: dict[str, Callable[[Actor, SourceReference], bool]] = {}
        self._lock = threading.RLock()
        self._reference_count = 0
        self._closed = False

    def register_provider(self, provider, validate):
        _identifier(provider)
        with self._lock:
            if self._closed or provider in self._validators or not callable(validate):
                raise SecurityDenied("来源提供方重复注册或不可用")
            if len(self._validators) >= self.capacity:
                raise SecurityDenied("来源提供方容量已满")
            self._validators[provider] = validate

    def begin(self, actor: Actor, *, fresh: bool):
        """fresh=True 是可信宿主的新上下文声明，不能从模型或客户端参数中透传。"""
        if not isinstance(actor, Actor) or fresh is not True:
            raise SecurityDenied("不能将未知历史初始化为公开来源")
        with self._lock:
            if self._closed or actor in self._states or actor in self._retired:
                raise SecurityDenied("上下文已存在、已失效或来源账本已关闭")
            if len(self._states) + len(self._retired) >= self.capacity:
                raise SecurityDenied("来源上下文容量已满")
            self._states[actor] = _State()
            return self.snapshot(actor)

    def _state(self, actor):
        if self._closed or not isinstance(actor, Actor) or actor not in self._states:
            raise SecurityDenied("可信来源状态不存在或已失效")
        return self._states[actor]

    def snapshot(self, actor: Actor) -> SourceSnapshot:
        with self._lock:
            state = self._state(actor)
            references = tuple(
                sorted(
                    state.references,
                    key=lambda r: (
                        r.provider,
                        r.scope,
                        r.unit_id,
                        r.revision,
                        r.level,
                        r.purpose,
                        r.presentation_version,
                        r.snapshot_digest,
                    ),
                )
            )
            return SourceSnapshot(actor, state.level, state.version, references)

    def _validate(self, actor, references):
        bindings = {}
        for reference in references:
            if not isinstance(reference, SourceReference) or reference.scope[0] != actor.tenant_id:
                raise SecurityDenied("记忆来源不属于当前租户")
            with self._lock:
                validator = self._validators.get(reference.provider)
            if reference.provider in bindings and bindings[reference.provider] is not validator:
                raise SecurityDenied("来源提供方在复查期间发生变化")
            bindings[reference.provider] = validator
            try:
                permitted = validator is not None and validator(actor, reference) is True
            except Exception:
                raise SecurityDenied("记忆来源或使用权限复查失败") from None
            if not permitted:
                raise SecurityDenied("记忆来源或使用权限已失效")
        return bindings

    def _providers_current(self, bindings):
        if any(self._validators.get(key) is not value for key, value in bindings.items()):
            raise SecurityDenied("来源提供方已撤销或重新装配")

    def record(self, actor: Actor, references: Iterable[SourceReference]) -> SourceSnapshot:
        references = tuple(islice(references, self.reference_capacity + 1))
        if len(references) > self.reference_capacity:
            raise SecurityDenied("来源引用容量已满")
        initial = self.snapshot(actor)
        bindings = self._validate(actor, references)
        return self._commit(actor, references, initial.version, bindings)

    def _commit(self, actor, references, expected, bindings):
        with self._lock:
            state = self._state(actor)
            self._providers_current(bindings)
            if state.version != expected or state.active:
                raise SecurityDenied("来源发生并发变更或已有外发在执行，请重试读取")
            merged = state.references | frozenset(references)
            added = len(merged) - len(state.references)
            if self._reference_count + added > self.reference_capacity:
                raise SecurityDenied("来源引用容量已满")
            if added:
                state.references = merged
                state.level = strongest(state.level, *(r.level for r in references))
                state.version = secrets.token_urlsafe(24)
                self._reference_count += added
            return self.snapshot(actor)

    def inherit(self, source: Actor, recipient: Actor, *, authorize_transfer):
        """只继承状态；消息正文交付仍需 BEFORE_AGENT_MESSAGE。不能代替接收方授权。"""
        if source.tenant_id != recipient.tenant_id:
            raise SecurityDenied("来源不能跨租户继承")
        previous, target = self.snapshot(source), self.snapshot(recipient)
        if authorize_transfer(source, recipient, previous) is not True:
            raise SecurityDenied("来源转交未获授权")
        bindings = self._validate(recipient, previous.references)
        with self._lock:
            if self._state(source).version != previous.version:
                raise SecurityDenied("移交过程中来源已改变")
            return self._commit(recipient, previous.references, target.version, bindings)

    def _checked(self, actor, version):
        snapshot = self.snapshot(actor)
        if snapshot.version != version:
            raise SecurityDenied("外发来源快照已过期")
        bindings = self._validate(actor, snapshot.references)
        return snapshot, bindings

    def _take(self, actor, version, bindings):
        with self._lock:
            state = self._state(actor)
            self._providers_current(bindings)
            if state.version != version:
                raise SecurityDenied("复查期间来源已改变")
            state.active += 1
            return state

    def verify(self, actor: Actor, version: str):
        """复核现有来源但不领取发送租约，供结果登记和历史恢复边界使用。"""
        snapshot, bindings = self._checked(actor, version)
        with self._lock:
            self._providers_current(bindings)
            if self._state(actor).version != version:
                raise SecurityDenied("来源复核期间状态已改变")
        return snapshot

    def _release(self, state):
        with self._lock:
            state.active -= 1

    @contextmanager
    def lease(self, actor: Actor, version: str):
        """执行前复查实际 SDK 权限与修订；发送期间拒绝向同一上下文交付新记忆。"""
        snapshot, bindings = self._checked(actor, version)
        state = self._take(actor, version, bindings)
        try:
            yield snapshot
        finally:
            self._release(state)

    @asynccontextmanager
    async def alease(self, actor: Actor, version: str):
        # 在线 SDK 复查在线程中执行；取消的线程绝不领取租约，不遗留 active 计数。
        snapshot, bindings = await asyncio.to_thread(self._checked, actor, version)
        state = self._take(actor, version, bindings)
        try:
            yield snapshot
        finally:
            self._release(state)

    def retire(self, actor: Actor):
        with self._lock:
            state = self._state(actor)
            if state.active:
                raise SecurityDenied("外发尚未结束，不能回收来源上下文")
            self._reference_count -= len(state.references)
            del self._states[actor]
            self._retired.add(actor)

    def unregister_provider(self, provider, validate):
        with self._lock:
            if self._closed or self._validators.get(provider) != validate:
                return
            del self._validators[provider]
            for state in self._states.values():
                if any(ref.provider == provider for ref in state.references):
                    state.version = secrets.token_urlsafe(24)

    def close(self):
        with self._lock:
            self._closed = True
            self._states.clear()
            self._retired.clear()
            self._validators.clear()
            self._reference_count = 0
