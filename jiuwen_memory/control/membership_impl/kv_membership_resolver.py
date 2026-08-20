"""KV 后端的空间授权事实读取（S09）。

一次判定所需的空间事实收敛成一次读取，既是正确性要求（归属对比的前提判定与两轴求值
必须看到同一份成员表），也是性能要求（判定链在全部数据面调用的前置路径上）。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import datetime, timezone

from jiuwen_memory.common.errors import NotFoundError
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.control.base import ControlOperatorType
from jiuwen_memory.control.membership import MembershipProducer, MembershipResolver
from jiuwen_memory.control.space import SpaceManager, SpaceProducer
from jiuwen_memory.control.space_impl.space_index import SpaceIndex
from jiuwen_memory.control.types import SpaceFacts, SpaceInfo, SpaceMember

_DEFAULT_TTL_SECONDS = 5.0
_DEFAULT_MAX_ENTRIES = 4096


def _now() -> datetime:
    return datetime.now(timezone.utc)


class KVMembershipResolver(MembershipResolver):
    """空间元数据与成员表的读取 + TTL 缓存 + 主体反查。

    缓存按 ``(org, space)`` 存整份事实：成员与策略是低频写、高频读，TTL 决定授权变更
    的最大生效延迟。后端异常直接向上抛，由鉴权点按拒绝处理——沿用过期结果即为放行方向
    的失效。本算子不读授权记录，那是判定实现自己的事。
    """

    def __init__(
        self,
        space: SpaceManager,
        index: SpaceIndex,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._space = space
        self._index = index
        self._ttl = max(float(ttl_seconds), 0.0)
        self._max_entries = max(int(max_entries), 1)
        self._cache: OrderedDict[tuple[str, str], tuple[float, SpaceFacts]] = OrderedDict()

    def operator_type(self) -> ControlOperatorType:
        return ControlOperatorType.MEMBERSHIP

    def health(self) -> None:
        self._space.health()

    def facts(self, org: str, space: str) -> SpaceFacts:
        key = (org, space)
        cached = self._cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            self._cache.move_to_end(key)
            return cached[1]
        info, members = self._space_snapshot(org, space)
        now = _now()
        live = tuple(
            member
            for member in members
            if member.expires_at is None or member.expires_at > now
        )
        facts = SpaceFacts(org=org, space=space, info=info, members=live)
        self._store(key, facts)
        return facts

    def spaces_for(self, actor: Scope, org: str) -> tuple[str, ...]:
        return self._index.spaces_for(actor, org)

    def invalidate(self, org: str, space: str | None = None) -> None:
        if space is not None:
            self._cache.pop((org, space), None)
            return
        for key in [k for k in self._cache if k[0] == org]:
            self._cache.pop(key, None)

    def _space_snapshot(self, org: str, space: str) -> tuple[SpaceInfo | None, list[SpaceMember]]:
        """取空间元数据与成员表；只走 ``SpaceManager`` 的抽象契约。

        空间不存在时元数据取 ``None``（隐式创建路径要区分「空间不存在」与「无成员」）。
        ``list_members`` 内会重复一次空间存在性点读，不为省这一次读引入契约外的读法：
        该路径只在缓存未命中时走到，TTL 内的重复调用不落到后端。
        """
        try:
            info = self._space.get(org, space)
        except NotFoundError:
            return None, []
        return info, list(self._space.list_members(org, space))

    def _store(self, key: tuple[str, str], facts: SpaceFacts) -> None:
        if self._ttl <= 0:
            return
        self._cache[key] = (time.monotonic() + self._ttl, facts)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)


@MembershipProducer.register("kv")
def _build(config):
    space = SpaceProducer.dep(config, default="kv")
    # 直取 SpaceManager 自己持有的那一份索引：另建一份即写入与读取分叉。
    return KVMembershipResolver(
        space,
        space.index,
        ttl_seconds=float(Factory.cfg_get(config, "ttl_seconds", _DEFAULT_TTL_SECONDS)),
        max_entries=int(Factory.cfg_get(config, "max_entries", _DEFAULT_MAX_ENTRIES)),
    )
